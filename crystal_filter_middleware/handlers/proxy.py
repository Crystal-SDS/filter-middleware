from crystal_filter_middleware.handlers import CrystalBaseHandler
from swift.common.swob import HTTPMethodNotAllowed
from swift.common.utils import public
import mimetypes
import operator
import json
import urllib


mappings = {'>': operator.gt, '>=': operator.ge,
            '==': operator.eq, '<=': operator.le, '<': operator.lt,
            '!=': operator.ne, "OR": operator.or_, "AND": operator.and_}


class CrystalProxyHandler(CrystalBaseHandler):

    def __init__(self, request, conf, app, logger):
        super(CrystalProxyHandler, self).__init__(request, conf,
                                                  app, logger)
        self.etag = None
        self.filter_exec_list = None

    def _get_dynamic_filters(self):
        # Dynamic binding of policies: using a Lua script that executes
        # a hgetall on the first matching key of a list and also returns
        # the global filters
        lua_sha = self.conf.get('LUA_get_pipeline_sha')
        args = (self.account.replace('AUTH_', ''), self.container)
        redis_list = self.redis.evalsha(lua_sha, 0, *args)
        index = redis_list.index("@@@@")  # Separator between pipeline and global filters

        self.filter_list = dict(zip(redis_list[0:index:2], redis_list[1:index:2]))
        self.global_filters = dict(zip(redis_list[index+1::2], redis_list[index+2::2]))

        self.proxy_filter_exec_list = {}
        self.object_filter_exec_list = {}

        if self.global_filters or self.filter_list:
            self.proxy_filter_exec_list = self._build_filter_execution_list('proxy')
            self.object_filter_exec_list = self._build_filter_execution_list('object')

    def _parse_vaco(self):
        return self.request.split_path(4, 4, rest_with_last=True)

    def _get_object_type(self):
        object_type = self.request.headers['Content-Type']
        if not object_type:
            object_type = mimetypes.guess_type(self.request.environ['PATH_INFO'])[0]
        return object_type

    def handle_request(self):

        if self.is_crystal_valid_request and hasattr(self, self.request.method):
            try:
                self._get_dynamic_filters()
                handler = getattr(self, self.request.method)
                getattr(handler, 'publicly_accessible')
            except AttributeError:
                return HTTPMethodNotAllowed(request=self.request)
            return handler()
        else:
            self.logger.info('Request disabled for Crystal')
            return self.request.get_response(self.app)

    def _check_size_type_tag(self, filter_metadata):

        correct_type = True
        correct_size = True
        correct_tag = True

        if filter_metadata['object_type']:
            obj_type = filter_metadata['object_type']
            correct_type = self._get_object_type() in \
                self.redis.lrange("object_type:" + obj_type, 0, -1)

        if filter_metadata['object_tag']:
            obj_tag = filter_metadata['object_tag']
            correct_tag = 'X-Object-Meta-'+obj_tag in self.request.headers

        if filter_metadata['object_size']:
            object_size = filter_metadata['object_size']
            op = mappings[object_size[0]]
            obj_lenght = int(object_size[1])
            correct_size = op(int(self.request.headers['Content-Length']),
                              obj_lenght)

        return correct_type and correct_size and correct_tag

    def _parse_filter_metadata(self, filter_metadata):
        """
        This method parses the filter metadata
        """
        filter_name = filter_metadata['filter_name']
        language = filter_metadata["language"]
        params = filter_metadata["params"]
        filter_type = filter_metadata["filter_type"]
        filter_main = filter_metadata["main"]
        filter_dep = filter_metadata["dependencies"]
        filter_size = filter_metadata["content_length"]
        reverse = filter_metadata["reverse"]

        filter_data = {'name': filter_name,
                       'language': language,
                       'params': self._parse_csv_params(params),
                       'reverse': reverse,
                       'type': filter_type,
                       'main': filter_main,
                       'dependencies': filter_dep,
                       'size': filter_size}

        return filter_data

    def _build_filter_execution_list(self, server):
        """
        This method builds the filter execution list (ordered).
        """
        filter_execution_list = {}

        ''' Parse global filters '''
        for _, filter_metadata in self.global_filters.items():
            filter_metadata = json.loads(filter_metadata)
            if self.method in filter_metadata and filter_metadata[self.method] \
               and filter_metadata['execution_server'] == server:
                filter_data = self._parse_filter_metadata(filter_metadata)
                order = filter_metadata["execution_order"]
                filter_execution_list[int(order)] = filter_data

        ''' Parse Project specific filters'''
        for _, filter_metadata in self.filter_list.items():
            filter_metadata = json.loads(filter_metadata)
            if self.method in filter_metadata and filter_metadata[self.method] \
               and filter_metadata['execution_server'] == server:
                filter_data = self._parse_filter_metadata(filter_metadata)
                order = filter_metadata["execution_order"]

                filter_execution_list[order] = filter_data

        return filter_execution_list

    def _format_crystal_metadata(self, crystal_md):
        """
        This method generates the metadata that will be stored alongside the
        object in the PUT requests. It allows the reverse case of the filters
        without querying the centralized controller.
        """
        for key in crystal_md["filter-list"].keys():
            cfilter = crystal_md["filter-list"][key]
            if cfilter['reverse']:
                current_params = cfilter['params']
                if current_params:
                    cfilter['params']['reverse'] = 'True'
                else:
                    cfilter['params'] = {'reverse': 'True'}

                cfilter['execution_server'] = cfilter['reverse']
                cfilter.pop('reverse')
            else:
                crystal_md["filter-list"].pop(key)

        return crystal_md

    def _set_crystal_metadata(self):
        """
        This method generates the metadata that will be stored alongside the
        object in the PUT requests. It allows the reverse case of the filters
        without querying the centralized controller.
        """
        filter_exec_list = {}
        for key in sorted(self.proxy_filter_exec_list.keys()):
            filter_exec_list[len(filter_exec_list)] = self.proxy_filter_exec_list[key]

        for key in sorted(self.object_filter_exec_list.keys()):
            filter_exec_list[len(filter_exec_list)] = self.object_filter_exec_list[key]

        metadata = {}
        metadata["original-etag"] = self.request.headers.get('ETag', '')
        metadata["original-size"] = self.request.headers.get('Content-Length', '')
        metadata["filter-list"] = filter_exec_list
        crystal_md = self._format_crystal_metadata(metadata)
        if crystal_md["filter-list"]:
            self.request.headers['X-Object-Sysmeta-Crystal'] = crystal_md

    def _parse_csv_params(self, csv_params):
        """
        Provides comma separated parameters "a=1,b=2" as a dictionary
        """
        # self.logger.info('csv_params: ' + csv_params)
        params_dict = dict()
        plist = csv_params.split(",")
        plist = filter(None, plist)  # Remove empty strings
        for p in plist:
            k, v = p.strip().split('=')
            params_dict[k] = v
        return params_dict

    def _parse_headers_params(self):
        """
        Extract parameters from headers
        """
        parameters = dict()
        for param in self.request.headers:
            if param.lower().startswith('x-crystal-parameter'):
                keyvalue = self.request.headers[param]
                keyvalue = urllib.unquote(keyvalue)
                [key, value] = keyvalue.split(':')
                parameters[key] = value
        return parameters

    @public
    def GET(self):
        """
        GET handler on Proxy
        """
        if self.proxy_filter_exec_list:
            self.logger.info('There are Filters to execute')
            self.logger.info(str(self.proxy_filter_exec_list))
            if 'Etag' in self.request.headers.keys():
                self.etag = self.request.headers.pop('Etag')
            self._build_pipeline(self.proxy_filter_exec_list)
        else:
            self.logger.info('No Filters to execute')

        if self.object_filter_exec_list:
            object_server_filters = json.dumps(self.object_filter_exec_list)
            self.request.headers['crystal_filters'] = object_server_filters

        response = self.request.get_response(self.app)

        if 'Content-Length' in response.headers:
            response.headers.pop('Content-Length')
        if 'Transfer-Encoding' in response.headers:
            response.headers.pop('Transfer-Encoding')

        if self.etag:
            response.headers['etag'] = self.etag

        return response

    @public
    def PUT(self):
        """
        PUT handler on Proxy
        """
        if self.proxy_filter_exec_list:
            self.logger.info('There are Filters to execute')
            self.logger.info(str(self.proxy_filter_exec_list))
            if 'Etag' in self.request.headers.keys():
                self.etag = self.request.headers.pop('Etag')
            self._set_crystal_metadata()
            self._build_pipeline(self.proxy_filter_exec_list)
        else:
            self.logger.info('No filters to execute')

        if self.object_filter_exec_list:
            object_server_filters = json.dumps(self.object_filter_exec_list)
            self.request.headers['crystal_filters'] = object_server_filters

        response = self.request.get_response(self.app)

        if self.etag:
            response.headers['Etag'] = self.etag

        return response

    @public
    def POST(self):
        """
        POST handler on Proxy
        """
        if self.proxy_filter_exec_list:
            self.logger.info('There are Filters to execute')
            self.logger.info(str(self.proxy_filter_exec_list))
            self._build_pipeline(self.proxy_filter_exec_list)
        else:
            self.logger.info('No filters to execute')

        if self.object_filter_exec_list:
            object_server_filters = json.dumps(self.object_filter_exec_list)
            self.request.headers['crystal_filters'] = object_server_filters

        return self.request.get_response(self.app)

    @public
    def HEAD(self):
        """
        HEAD handler on Proxy
        """
        if self.proxy_filter_exec_list:
            self.logger.info('There are Filters to execute')
            self.logger.info(str(self.proxy_filter_exec_list))
            self._build_pipeline(self.proxy_filter_exec_list)
        else:
            self.logger.info('No filters to execute')

        if self.object_filter_exec_list:
            object_server_filters = json.dumps(self.object_filter_exec_list)
            self.request.headers['crystal_filters'] = object_server_filters

        return self.request.get_response(self.app)

    @public
    def DELETE(self):
        """
        DELETE handler on Proxy
        """
        if self.proxy_filter_exec_list:
            self.logger.info('There are Filters to execute')
            self.logger.info(str(self.proxy_filter_exec_list))
            self._build_pipeline(self.proxy_filter_exec_list)
        else:
            self.logger.info('No filters to execute')

        if self.object_filter_exec_list:
            object_server_filters = json.dumps(self.object_filter_exec_list)
            self.request.headers['crystal_filters'] = object_server_filters

        return self.request.get_response(self.app)
