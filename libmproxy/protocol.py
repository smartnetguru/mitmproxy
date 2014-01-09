from libmproxy import flow
from libmproxy.utils import timestamp
from netlib import http, utils, tcp
from netlib.odict import ODictCaseless

KILL = 0 # FIXME: Remove duplication with proxy module
LEGACY = True

#FIXME: Combine with ProxyError?
class ProtocolError(Exception):
    def __init__(self, code, msg, headers=None):
        self.code, self.msg, self.headers = code, msg, headers

    def __str__(self):
        return "ProtocolError(%s, %s)"%(self.code, self.msg)


def _handle(msg, conntype, connection_handler, *args, **kwargs):
    handler = None
    if conntype == "http":
        handler = HTTPHandler(connection_handler)
    else:
        raise NotImplementedError

    f = getattr(handler, "handle_" + msg)
    return f(*args, **kwargs)


def handle_messages(conntype, connection_handler):
    _handle("messages", conntype, connection_handler)


def handle_error(conntype, connection_handler, e):
    _handle("error", conntype, connection_handler, e)


class ConnectionTypeChange(Exception):
    pass


class ProtocolHandler(object):
    def __init__(self, c):
        self.c = c


class Flow(object):
    def __init__(self, client_conn, server_conn, timestamp_start, timestamp_end):
        self.client_conn, self.server_conn = client_conn, server_conn
        self.timestamp_start, self.timestamp_end = timestamp_start, timestamp_end


class HTTPFlow(Flow):
    def __init__(self, client_conn, server_conn, timestamp_start, timestamp_end, request, response):
        Flow.__init__(self, client_conn, server_conn,
                      timestamp_start, timestamp_end)
        self.request, self.response = request, response


class HTTPResponse(object):
    def __init__(self, http_version, code, msg, headers, content, timestamp_start, timestamp_end):
        self.http_version = http_version
        self.code = code
        self.msg = msg
        self.headers = headers
        self.content = content
        self.timestamp_start = timestamp_start
        self.timestamp_end = timestamp_end

        assert isinstance(headers, ODictCaseless)

    #FIXME: Legacy
    @property
    def request(self):
        return False

    def _assemble(self):
        response_line = 'HTTP/%s.%s %s %s'%(self.http_version[0], self.http_version[1], self.code, self.msg)
        return '%s\r\n%s\r\n%s' % (response_line, str(self.headers), self.content)

    @classmethod
    def from_stream(cls, rfile, request_method, include_content=True, body_size_limit=None):
        """
        Parse an HTTP response from a file stream
        """
        if not include_content:
            raise NotImplementedError

        timestamp_start = timestamp()
        http_version, code, msg, headers, content = http.read_response(
            rfile,
            request_method,
            body_size_limit)
        timestamp_end = timestamp()
        return HTTPResponse(http_version, code, msg, headers, content, timestamp_start, timestamp_end)

class HTTPRequest(object):
    def __init__(self, form_in, method, scheme, host, port, path, http_version, headers, content,
                 timestamp_start, timestamp_end, form_out=None, ip=None):
        self.form_in = form_in
        self.method = method
        self.scheme = scheme
        self.host = host
        self.port = port
        self.path = path
        self.http_version = http_version
        self.headers = headers
        self.content = content
        self.timestamp_start = timestamp_start
        self.timestamp_end = timestamp_end

        self.form_out = form_out or self.form_in
        self.ip = ip # resolved ip address
        assert isinstance(headers, ODictCaseless)

    #FIXME: Remove, legacy
    def is_live(self):
        return True

    def _assemble(self):
        request_line = None
        if self.form_out == "asterisk" or self.form_out == "origin":
            request_line = '%s %s HTTP/%s.%s' % (self.method, self.path, self.http_version[0], self.http_version[1])
        else:
            raise NotImplementedError
        return '%s\r\n%s\r\n%s' % (request_line, str(self.headers), self.content)

    @classmethod
    def from_stream(cls, rfile, include_content=True, body_size_limit=None):
        """
        Parse an HTTP request from a file stream
        """
        http_version, host, port, scheme, method, path, headers, content, timestamp_start, timestamp_end \
            = None, None, None, None, None, None, None, None, None, None

        timestamp_start = timestamp()
        request_line = HTTPHandler.get_line(rfile)

        request_line_parts = http.parse_init(request_line)
        if not request_line_parts:
            raise ProtocolError(400, "Bad HTTP request line: %s"%repr(request_line))
        method, path, http_version = request_line_parts

        if path == '*':
            form_in = "asterisk"
        elif path.startswith("/"):
            form_in = "origin"
            if not utils.isascii(path):
                raise ProtocolError(400, "Bad HTTP request line: %s"%repr(request_line))
        elif method.upper() == 'CONNECT':
            form_in = "authority"
            r = http.parse_init_connect(request_line)
            if not r:
                raise ProtocolError(400, "Bad HTTP request line: %s"%repr(request_line))
            host, port, _ = r
        else:
            form_in = "absolute"
            r = http.parse_init_proxy(request_line)
            if not r:
                raise ProtocolError(400, "Bad HTTP request line: %s"%repr(request_line))
            _, scheme, host, port, path, _ = r

        headers = http.read_headers(rfile)
        if headers is None:
            raise ProtocolError(400, "Invalid headers")

        if include_content:
            content = http.read_http_body(rfile, headers, body_size_limit, True)
            timestamp_end = timestamp()

        return HTTPRequest(form_in, method, scheme, host, port, path, http_version, headers, content,
                           timestamp_start, timestamp_end)


class HTTPHandler(ProtocolHandler):

    def handle_messages(self):
        while self.handle_request():
            pass
        self.c.close = True

    def handle_error(self, e):
        raise e # FIXME: Proper error handling

    def handle_request(self):
        try:
            flow = HTTPFlow(self.c.client_conn, self.c.server_conn, timestamp(), None, None, None)
            flow.request = self.read_request()
            request_reply = self.c.channel.ask("request" if LEGACY else "httprequest", flow.request)

            if request_reply is None or request_reply == KILL:
                    return False
            if isinstance(request_reply, HTTPResponse):
                flow.response = request_reply
            else:
                flow.request = request_reply
                raw = flow.request._assemble()
                self.c.server_conn.wfile.write(raw)
                self.c.server_conn.wfile.flush()
                flow.response = self.read_response(flow)
            response_reply = self.c.channel.ask("response" if LEGACY else "httpresponse", flow.response)
            if response_reply is None or response_reply == KILL:
                return False
            else:
                raw = flow.response._assemble()
                self.c.client_conn.wfile.write(raw)
                self.c.client_conn.wfile.flush()

            if (http.connection_close(flow.request.http_version, flow.request.headers) or
                    http.connection_close(flow.response.http_version, flow.response.headers)):
                return False

            flow.timestamp_end = timestamp()
            return flow
        except tcp.NetLibDisconnect, e:
            return False

    def read_request(self):
        request = HTTPRequest.from_stream(self.c.client_conn.rfile, body_size_limit=self.c.config.body_size_limit)

        if self.c.mode == "regular":
            self.authenticate(request)
            if request.form_in == "authority":
                if not self.c.config.forward_proxy:
                    self.c.establish_server_connection(request.host, request.port)
                    self.c.client_conn.wfile.write(
                        'HTTP/1.1 200 Connection established\r\n' +
                        ('Proxy-agent: %s\r\n'%self.c.server_version) +
                        '\r\n'
                    )
                    self.c.client_conn.wfile.flush()

                self.c.handle_ssl()
                self.c.mode = "transparent"
                self.c.determine_conntype()
                # FIXME: We need to persist the CONNECT request
                raise ConnectionTypeChange
            elif request.form_in == "absolute":
                if not self.c.config.forward_proxy:
                    request.form_out = "origin"
                    if ((not self.c.server_conn) or
                            (self.c.server_conn.address != (request.host, request.port))):
                        self.c.establish_server_connection(request.host, request.port)
            else:
                raise ProtocolError(400, "Invalid Request")

        return request

    def read_response(self, flow):
        return HTTPResponse.from_stream(self.c.server_conn.rfile, flow.request.method, body_size_limit=self.c.config.body_size_limit)

    def authenticate(self, request):
        if self.c.config.authenticator:
            if self.c.config.authenticator.authenticate(request.headers):
                self.c.config.authenticator.clean(request.headers)
            else:
                raise ProtocolError(
                    407,
                    "Proxy Authentication Required",
                    self.c.config.authenticator.auth_challenge_headers()
                )
        return request.headers

    @staticmethod
    def get_line(fp):
        """
            Get a line, possibly preceded by a blank.
        """
        line = fp.readline()
        if line == "\r\n" or line == "\n": # Possible leftover from previous message
            line = fp.readline()
        if line == "":
            raise tcp.NetLibDisconnect
        return line