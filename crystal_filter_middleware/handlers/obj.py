from crystal_filter_middleware.handlers import CrystalBaseHandler
from swift.common.swob import HTTPMethodNotAllowed
from swift.common.utils import public
import json


class CrystalObjectHandler(CrystalBaseHandler):

    def __init__(self, request, conf, app, logger, filter_control):
        super(CrystalObjectHandler, self).__init__(request, conf,
                                                   app, logger,
                                                   filter_control)

        self.device = self.request.environ['PATH_INFO'].split('/', 2)[1]

    def _parse_vaco(self):
        _, _, acc, cont, obj = self.request.split_path(
            5, 5, rest_with_last=True)
        return ('0', acc, cont, obj)

    @property
    def is_slo_get_request(self):
        """
        Determines from a GET request and its  associated response
        if the object is a SLO
        """
        return self.request.params.get('multipart-manifest') == 'get'

    def handle_request(self):
        if self.is_crystal_object_put:
            return self.request.get_response(self.app)

        if hasattr(self, self.request.method):
            try:
                handler = getattr(self, self.request.method)
                getattr(handler, 'publicly_accessible')
            except AttributeError:
                return HTTPMethodNotAllowed(request=self.request)
            return handler()
        else:
            return self.request.get_response(self.app)
            # un-defined method should be NOT ALLOWED
            # return HTTPMethodNotAllowed(request=self.request)

    def _augment_filter_execution_list(self, filter_list):
        new_storlet_list = {}

        # Reverse execution
        if filter_list:
            for key in reversed(sorted(filter_list)):
                launch_key = len(new_storlet_list.keys())
                new_storlet_list[launch_key] = filter_list[key]

        # Get filter list to execute from proxy server
        if 'crystal/filters' in self.request.headers:
            req_filter_list = eval(self.request.headers.pop('crystal/filters'))
            for key in sorted(req_filter_list, reverse=True):
                launch_key = len(new_storlet_list.keys())
                new_storlet_list[launch_key] = req_filter_list[key]

        return new_storlet_list

    @public
    def GET(self):
        """
        GET handler on Object
        """
        response = self.request.get_response(self.app)
        
        if 'X-Object-Sysmeta-Crystal' in response.headers:
            crystal_md = eval(response.headers.pop('X-Object-Sysmeta-Crystal'))
            response.headers['ETag'] = crystal_md['original-etag']
            response.headers['Content-Length'] = crystal_md['original-size']
            filter_list = crystal_md.get('filter-list', None)
            filter_exec_list = self._augment_filter_execution_list(filter_list)
            response = self.apply_filters_on_get(response, filter_exec_list)
            
        return response

    @public
    def PUT(self):
        """
        PUT handler on Object Server
        """
        # IF 'crystal/filters' is in headers, means that is needed to run a
        # Filter on Object Server before store the object.
        if 'crystal/filters' in self.request.headers:
            self.logger.info('Crystal Filters - There are filters to execute')
            filter_list = json.loads(self.request.headers['crystal/filters'])
            self.apply_filters_on_put(filter_list)

        return self.request.get_response(self.app)