import requests
from decimal import Decimal, InvalidOperation
from osgeo_importer.handlers import ImportHandlerMixin, GetModifiedFieldsMixin
from geoserver.catalog import FailedRequestError
from osgeo_importer.handlers import ensure_can_run
from django import db
from geonode.geoserver.helpers import gs_catalog
from geoserver.support import DimensionInfo
import re
import os


def configure_time(resource, name='time', enabled=True, presentation='LIST', resolution=None, units=None,
                   unitSymbol=None, **kwargs):
    """
    Configures time on a geoserver resource.
    """
    time_info = DimensionInfo(name, enabled, presentation, resolution, units, unitSymbol, **kwargs)
    resource.metadata = {'time': time_info}
    return resource.catalog.save(resource)


class GeoServerTimeHandler(GetModifiedFieldsMixin, ImportHandlerMixin):
    """
    Enables time in Geoserver for a layer.
    """

    def can_run(self, layer, layer_config, *args, **kwargs):
        """
        Returns true if the configuration has enough information to run the handler.
        """

        if not layer_config.get('configureTime', None):
            return False

        if not any([layer_config.get('start_date', None), layer_config.get('end_date', None)]):
            return False

        return True

    @ensure_can_run
    def handle(self, layer, layer_config, *args, **kwargs):
        """
        Configures time on the object.

        Handler specific params:
        "configureTime": Must be true for this handler to run.
        "start_date": Passed as the start time to Geoserver.
        "end_date" (optional): Passed as the end attribute to Geoserver.
        """

        lyr = gs_catalog.get_layer(layer)
        self.update_date_attributes(layer_config)
        configure_time(lyr.resource, attribute=layer_config.get('start_date'),
                       end_attribute=layer_config.get('start_date'))


class GeoserverPublishHandler(ImportHandlerMixin):
    catalog = gs_catalog
    workspace = 'geonode'
    srs = 'EPSG:4326'

    def can_run(self, layer, layer_config, *args, **kwargs):
        """
        Returns true if the configuration has enough information to run the handler.
        """
        if re.search(r'\.tif$', layer):
            return False

        return True

    def get_default_store(self):
        connection = db.connections['datastore']
        settings = connection.settings_dict

        return {
              'database': settings['NAME'],
              'passwd': settings['PASSWORD'],
              'namespace': 'http://www.geonode.org/',
              'type': 'PostGIS',
              'dbtype': 'postgis',
              'host': settings['HOST'],
              'user': settings['USER'],
              'port': settings['PORT'],
              'enabled': 'True',
              'name': settings['NAME']}

    def get_or_create_datastore(self, layer_config):
        connection_string = layer_config.get('geoserver_store', self.get_default_store())

        try:
            return self.catalog.get_store(connection_string['name'])
        except FailedRequestError:
            store = self.catalog.create_datastore(connection_string['name'], workspace=self.workspace)
            store.connection_parameters.update(connection_string)
            self.catalog.save(store)

        return self.catalog.get_store(connection_string['name'])

    def geogig_handler(self, store, layer, layer_config):

        repo = store.connection_parameters['geogig_repository']
        auth = (self.catalog.username, self.catalog.password)
        repo_url = self.catalog.service_url.replace('/rest', '/geogig/{0}/'.format(repo))
        transaction = requests.get(repo_url + 'beginTransaction.json', auth=auth)
        transaction_id = transaction.json()['response']['Transaction']['ID']
        params = self.get_default_store()
        params['password'] = params['passwd']
        params['table'] = layer
        params['transactionId'] = transaction_id

        import_command = requests.get(repo_url + 'postgis/import.json', params=params, auth=auth)
        task = import_command.json()['task']

        status = 'NOT RUN'
        while status != 'FINISHED':
            check_task = requests.get(task['href'], auth=auth)
            status = check_task.json()['task']['status']

        if check_task.json()['task']['status'] == 'FINISHED':
            requests.get(repo_url + 'add.json', params={'transactionId': transaction_id}, auth=auth)
            requests.get(repo_url + 'commit.json', params={'transactionId': transaction_id}, auth=auth)
            requests.get(repo_url + 'endTransaction.json', params={'transactionId': transaction_id}, auth=auth)

    @ensure_can_run
    def handle(self, layer, layer_config, *args, **kwargs):
        """
        Publishes a layer to GeoServer.

        Handler specific params:
        "geoserver_store": Connection parameters used to get/create the geoserver store.

        """

        store = self.get_or_create_datastore(layer_config)

        if getattr(store, 'type', '').lower() == 'geogig':
            self.geogig_handler(store, layer, layer_config)

        self.catalog.publish_featuretype(layer, self.get_or_create_datastore(layer_config), self.srs)
        return True


class GeoserverPublishCoverageHandler(ImportHandlerMixin):
    catalog = gs_catalog
    workspace = 'geonode'

    def can_run(self, layer, layer_config, *args, **kwargs):
        """
        Returns true if the configuration has enough information to run the handler.
        """
        if re.search(r'\.tif$', layer):
            return True

        return False

    @ensure_can_run
    def handle(self, layer, layer_config, *args, **kwargs):
        """
        Publishes a Coverage layer to GeoServer.
        """
        name = os.path.splitext(os.path.basename(layer))[0]
        workspace = self.catalog.get_workspace(self.workspace)
        self.catalog.create_coveragestore(name, layer, workspace, True)
        return True


class GeoWebCacheHandler(ImportHandlerMixin):
    """
    Configures GeoWebCache for a layer in Geoserver.
    """
    catalog = gs_catalog
    workspace = 'geonode'

    @staticmethod
    def config(**kwargs):
        return """<?xml version="1.0" encoding="UTF-8"?>
            <GeoServerLayer>
              <name>{name}</name>
              <enabled>true</enabled>
              <mimeFormats>
                <string>image/png</string>
                <string>image/jpeg</string>
                <string>image/png8</string>
              </mimeFormats>
              <gridSubsets>
                <gridSubset>
                  <gridSetName>EPSG:900913</gridSetName>
                </gridSubset>
                <gridSubset>
                  <gridSetName>EPSG:4326</gridSetName>
                </gridSubset>
                <gridSubset>
                  <gridSetName>EPSG:3857</gridSetName>
                </gridSubset>
              </gridSubsets>
              <metaWidthHeight>
                <int>4</int>
                <int>4</int>
              </metaWidthHeight>
              <expireCache>0</expireCache>
              <expireClients>0</expireClients>
              <parameterFilters>
                {regex_parameter_filter}
                <styleParameterFilter>
                  <key>STYLES</key>
                  <defaultValue/>
                </styleParameterFilter>
              </parameterFilters>
              <gutter>0</gutter>
            </GeoServerLayer>""".format(**kwargs)

    def can_run(self, layer, layer_config, *args, **kwargs):
        """
        Only run this handler if the layer is found in Geoserver.
        """
        self.layer = self.catalog.get_layer(layer)

        if self.layer:
            return True

        return

    @staticmethod
    def time_enabled(layer):
        """
        Returns True is time is enabled for a Geoserver layer.
        """
        return 'time' in (getattr(layer.resource, 'metadata', []) or [])

    def gwc_url(self, layer):
        """
        Returns the GWC URL given a Geoserver layer.
        """

        return self.catalog.service_url.replace('rest', 'gwc/rest/layers/{workspace}:{layer_name}.xml'.format(
            workspace=layer.resource.workspace.name, layer_name=layer.name))

    @ensure_can_run
    def handle(self, layer, layer_config, *args, **kwargs):
        """
        Adds a layer to GWC.
        """
        regex_filter = ""
        time_enabled = self.time_enabled(self.layer)

        if time_enabled:
            regex_filter = """
                <regexParameterFilter>
                  <key>TIME</key>
                  <defaultValue/>
                  <regex>.*</regex>
                </regexParameterFilter>
                """

        return self.catalog.http.request(self.gwc_url(self.layer), method="POST",
                                         body=self.config(regex_parameter_filter=regex_filter, name=self.layer.name))


class GeoServerBoundsHandler(ImportHandlerMixin):
    """
    Sets the lat/long bounding box of a layer to the max extent of WGS84 if the values of the current lat/long
    bounding box fail the Decimal quantize method (which Django uses internally when validating decimals).

    This can occur when the native bounding box contain Infinity values.
    """

    catalog = gs_catalog
    workspace = 'geonode'

    def can_run(self, layer, layer_config, *args, **kwargs):
        """
        Only run this handler if the layer is found in Geoserver.
        """
        self.catalog._cache.clear()
        self.layer = self.catalog.get_layer(layer)

        if self.layer:
            return True

        return

    @ensure_can_run
    def handle(self, layer, layer_config, *args, **kwargs):
        resource = self.layer.resource
        try:
            for dec in map(Decimal, resource.latlon_bbox[:4]):
                dec.quantize(1)

        except InvalidOperation:
            resource.latlon_bbox = ['-180', '180', '-90', '90', 'EPSG:4326']
            self.catalog.save(resource)
