"""Authenticated loopback HTTP handoff with one durable, non-replaying worker."""
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import threading
from urllib.parse import unquote
import uuid

from integrations.model_endpoint import EndpointError, strict_json
from integrations.skill_rsi_contract import MAX_REQUEST_BYTES, RSIError
from integrations.skill_rsi_versions import SkillVersions
from integrations.skill_rsi_catalog import tool_catalog


def canonical_id(value):
    try:
        if str(uuid.UUID(value)) == value:
            return value
    except (ValueError, TypeError, AttributeError):
        pass
    raise RSIError('invalid_request_id')


def skill_path_identity(value):
    """Decode one URL component, preserving the exact skill-v1 identity."""
    if (not value or not value.isascii() or any(c in value for c in '/?#')
        or re.search(r'%(?![a-fA-F0-9]{2})',value)):
        raise RSIError('invalid_skill_identity')
    try:
        return unquote(value,encoding='utf-8',errors='strict')
    except UnicodeError:
        raise RSIError('invalid_skill_identity') from None


class SkillHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, service, token, *, host='127.0.0.1', port=18820):
        if host != '127.0.0.1' or not isinstance(token,str) or not 32 <= len(token) <= 256 or any(not 33<=ord(c)<=126 for c in token):
            raise RSIError('invalid_local_server_configuration')
        self.service, self.token = service, token
        self.versions = SkillVersions(service.root)
        self.admission = threading.Lock()
        self.connections = threading.BoundedSemaphore(8)
        self.active = None
        self.worker = None
        super().__init__((host,port), SkillHandler)

    def process_request(self, request, client_address):
        if not self.connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        request.settimeout(10)
        try:
            super().process_request(request,client_address)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request,client_address)
        finally:
            self.connections.release()

    def handle_error(self, request, client_address):
        pass  # Fixed HTTP responses and durable state carry errors; never dump private requests.

    def submit(self, body, *, expected_executor_id=None):
        request = self.service.validate(body)
        with self.admission:
            if expected_executor_id is not None and expected_executor_id!=self.service.executor_id:
                raise RSIError('executor_precondition_failed')
            try:
                self.service.store.get(request['request_id'])
            except RSIError as exc:
                if exc.code != 'evaluation_not_found':
                    raise
            else:
                _, _, job = self.service.prepare(request)
                return 200, job
            if self.active is not None:
                raise RSIError('executor_busy')
            if self.service.store.unresolved(request['request_id']):
                raise RSIError('previous_evaluation_unresolved')
            request, created, job = self.service.prepare(request)
            if not created:
                return 200, job
            self.active = request['request_id']
            self.worker = threading.Thread(target=self._run, args=(request,), name='skill-rsi-evaluation',daemon=False)
            try:
                self.worker.start()
            except Exception:
                self.active = None
                self.service.store.transition(request['request_id'],'accepted','rejected',{'error':'worker_start_failed'})
                raise RSIError('worker_start_failed') from None
            return 202, job

    def _run(self, request):
        try:
            self.service._execute_reserved(request)
        except Exception:
            # Do not emit request bodies, endpoint details or exception strings to HTTP logs.
            job = self.service.store.get(request['request_id'])
            if job['state'] in ('accepted','running'):
                self.service.store.transition(request['request_id'],job['state'],'unknown',{'error':'worker_interrupted'})
        finally:
            with self.admission:
                self.active = None

    def wait_worker(self):
        worker = self.worker
        if worker is not None:
            worker.join()  # Explicit graceful operator shutdown; never kill a potentially writing task.


class SkillHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    server_version = 'SkillRSI/1'

    def log_message(self, *args):
        pass

    def reply(self, status, value):
        raw = json.dumps(value,ensure_ascii=True,allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers()
        try:
            self.wfile.write(raw)
        except OSError:
            pass  # The durable worker continues if the client disconnects.

    def authorized(self):
        if self.headers.get('Origin') is not None:
            self.reply(403,{'error':'browser_origin_not_allowed'})
            return False
        auth = self.headers.get_all('Authorization',[])
        if len(auth) != 1 or not hmac.compare_digest(auth[0].encode(),('Bearer '+self.server.token).encode()):
            self.reply(401,{'error':'unauthorized'})
            return False
        return True

    def do_GET(self):
        if not self.authorized():
            return
        try:
            if self.path == '/v1/capabilities':
                self.reply(200,{'schema_version':'skill-rsi-capabilities-v1', 'tasks':self.server.service.tasks,
                    'executor_id':self.server.service.executor_id, 'executor':self.server.service.descriptor,
                    'skill_schema':'skill-v1','max_active_evaluations':1,'automatic_replay':False,
                    'executor_precondition_supported':True,
                    'tool_catalog_route':'/v1/tool-catalogs/{task_id}',
                    'skill_head_route':'/v1/skill-heads/{skill_id}'})
            elif self.path.startswith('/v1/evaluations/'):
                identity = canonical_id(self.path[len('/v1/evaluations/'):])
                self.reply(200,self.server.service.store.get(identity))
            elif self.path.startswith('/v1/versions/'):
                self.reply(200,self.server.versions.describe(self.path[len('/v1/versions/'):]))
            elif self.path.startswith('/v1/skill-heads/'):
                identity = skill_path_identity(self.path[len('/v1/skill-heads/'):])
                self.reply(200,self.server.versions.current_head(identity))
            elif self.path.startswith('/v1/tool-catalogs/'):
                self.reply(200,tool_catalog(self.server.service,self.path[len('/v1/tool-catalogs/'):]))
            else:
                self.reply(404,{'error':'route_not_found'})
        except RSIError as exc:
            self.reply(404 if exc.code in ('evaluation_not_found','skill_version_not_found') else 400,{'error':exc.code})
        except Exception:
            self.reply(503,{'error':'service_unavailable'})

    def do_POST(self):
        if not self.authorized():
            return
        if self.path not in ('/v1/evaluations','/v1/versions'):
            self.reply(404,{'error':'route_not_found'})
            return
        try:
            lengths = self.headers.get_all('Content-Length',[])
            if (len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit()
                or self.headers.get('Transfer-Encoding') is not None):
                raise RSIError('invalid_body_length')
            size = int(lengths[0])
            if not 1 <= size <= MAX_REQUEST_BYTES:
                self.reply(413,{'error':'request_size_limit'})
                return
            if self.headers.get('Content-Type') != 'application/json':
                self.reply(415,{'error':'json_content_type_required'})
                return
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise RSIError('incomplete_request_body')
            body = strict_json(raw)
            precondition=None
            conditions=self.headers.get_all('If-Match',[])
            if conditions:
                if (self.path!='/v1/evaluations' or len(conditions)!=1
                    or not re.fullmatch(r'"[a-f0-9]{64}"',conditions[0])):
                    raise RSIError('invalid_executor_precondition')
                precondition=conditions[0][1:-1]
            status, job = ((200,self.server.versions.register(body)) if self.path == '/v1/versions'
                           else self.server.submit(body,expected_executor_id=precondition))
            self.reply(status,job)
        except EndpointError:
            self.reply(400,{'error':'invalid_request_json'})
        except RSIError as exc:
            status = 429 if exc.code in ('executor_busy','skill_registry_busy') else 409 if exc.code in ('request_id_conflict','previous_evaluation_unresolved','skill_version_conflict','executor_precondition_failed') else 400
            self.reply(status,{'error':exc.code})
        except Exception:
            self.reply(503,{'error':'service_unavailable'})
