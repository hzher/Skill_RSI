"""Small loopback RSI client: one-shot writes, pinned execution, bounded polling."""
import http.client
import json
import math
import re
import socket
import threading
import time
from urllib.parse import quote, urlsplit
import uuid

from integrations.model_endpoint import EndpointError, strict_json
from integrations.skill_rsi_contract import MAX_REQUEST_BYTES, RSIError, skill_version

MAX_REPLY_BYTES = 1024 * 1024
STATES = frozenset(('accepted','running','completed','rejected','unknown'))
REMOTE_CODES = frozenset(('unauthorized','browser_origin_not_allowed','request_id_conflict',
    'previous_evaluation_unresolved','executor_busy','skill_registry_busy','skill_version_conflict',
    'executor_precondition_failed','invalid_executor_precondition','evaluation_not_found',
    'skill_version_not_found','matching_tool_catalog_unavailable','service_unavailable',
    'task_not_available','request_size_limit','skill_head_binding_invalid'))


class RSIClientError(ValueError):
    """Only fixed local/remote categories, never response bodies or credentials."""
    def __init__(self, code, *, http_status=None):
        super().__init__(code)
        self.code, self.http_status = code, http_status


def _uuid(value):
    try:
        if not isinstance(value,str) or str(uuid.UUID(value))!=value:raise ValueError()
    except (ValueError,TypeError,AttributeError):
        raise RSIClientError('invalid_request_id') from None
    return value


def _digest(value):
    if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{64}',value):
        raise RSIClientError('invalid_executor_or_version')
    return value


def _duration(value, maximum):
    if type(value) not in (int,float) or not 0<value<=maximum or not math.isfinite(value):
        raise RSIClientError('invalid_client_budget')
    return value


def _component(value):
    try:
        if not isinstance(value,str) or not value or len(value.encode('utf-8'))>MAX_REQUEST_BYTES:
            raise ValueError()
        return quote(value,safe='')
    except (ValueError,UnicodeError):
        raise RSIClientError('invalid_path_identity') from None


def _body_bytes(value):
    try:raw=json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
    except (ValueError,TypeError,UnicodeError,RecursionError):
        raise RSIClientError('invalid_request_json') from None
    if len(raw)>MAX_REQUEST_BYTES:raise RSIClientError('request_size_limit')
    return raw


def _snapshot(value):
    try:return strict_json(_body_bytes(value))
    except EndpointError:raise RSIClientError('invalid_request_json') from None


class SkillRSIClient:
    def __init__(self, token, *, base_url='http://127.0.0.1:18820', executor_id=None, timeout_seconds=10):
        try:
            parts=urlsplit(base_url)
            if (parts.scheme!='http' or parts.hostname!='127.0.0.1' or parts.username is not None
                or parts.password is not None or parts.path not in ('','/') or parts.query or parts.fragment):
                raise ValueError()
            port=parts.port if parts.port is not None else 80
            if not 1<=port<=65535:raise ValueError()
        except (ValueError,TypeError,AttributeError):
            raise RSIClientError('invalid_local_server_address') from None
        if (not isinstance(token,str) or not 32<=len(token)<=256
            or any(not 33<=ord(c)<=126 for c in token)):
            raise RSIClientError('invalid_local_access_token')
        self.host, self.port = '127.0.0.1',port
        self._token=token
        self.executor_id=_digest(executor_id) if executor_id is not None else None
        self.timeout_seconds=_duration(timeout_seconds,30)
        self._capabilities_seen=False

    def _request(self, method, path, body=None, *, expected=(200,), timeout=None, pin=False):
        raw=None
        if body is not None:
            raw=_body_bytes(body)
        headers={'Authorization':'Bearer '+self._token,'Accept':'application/json'}
        if raw is not None:headers['Content-Type']='application/json'
        if pin:headers['If-Match']='"'+_digest(self.executor_id)+'"'
        budget=self.timeout_seconds if timeout is None else timeout
        deadline=time.monotonic()+budget
        connection=http.client.HTTPConnection(self.host,self.port,timeout=budget)
        response=None
        wire_socket=None
        expired=threading.Event()
        def expire_connection():
            expired.set()
            owned=wire_socket if wire_socket is not None else connection.sock
            if owned is not None:
                try:owned.shutdown(socket.SHUT_RDWR)
                except OSError:pass
        timer=threading.Timer(budget,expire_connection);timer.daemon=True;timer.start()
        try:
            connection.connect()
            wire_socket=connection.sock
            def remaining_budget():
                remaining=deadline-time.monotonic()
                if expired.is_set() or remaining<=0:raise TimeoutError()
                # HTTP/1.0 transfers socket ownership to the response file;
                # our reference still sets the timeout on that same live FD.
                if wire_socket is not None:wire_socket.settimeout(remaining)
            remaining_budget()
            connection.request(method,path,body=raw,headers=headers)
            remaining_budget()
            response=connection.getresponse()
            remaining_budget()
            status=response.status
            if 300<=status<400:raise RSIClientError('redirect_not_allowed',http_status=status)
            if response.getheader('Content-Type','').split(';',1)[0].strip().lower()!='application/json':
                raise RSIClientError('invalid_response_format',http_status=status)
            reply=bytearray()
            while len(reply)<=MAX_REPLY_BYTES:
                remaining_budget()
                chunk=response.read1(min(65536,MAX_REPLY_BYTES+1-len(reply)))
                if not chunk:break
                reply.extend(chunk)
                if response.isclosed():break
            if expired.is_set() or time.monotonic()>=deadline:raise TimeoutError()
            if len(reply)>MAX_REPLY_BYTES:raise RSIClientError('response_size_limit',http_status=status)
            try:value=strict_json(reply)
            except EndpointError:raise RSIClientError('invalid_response_json',http_status=status) from None
            if not isinstance(value,dict):raise RSIClientError('invalid_response_shape',http_status=status)
            if status not in expected:
                category=value.get('error')
                code=category if isinstance(category,str) and category in REMOTE_CODES else 'remote_request_rejected'
                raise RSIClientError(code,http_status=status)
            return value
        except TimeoutError:
            raise RSIClientError('submission_outcome_unknown' if method=='POST' else 'query_timeout') from None
        except (OSError,http.client.HTTPException):
            if method!='POST' and (expired.is_set() or time.monotonic()>=deadline):
                raise RSIClientError('query_timeout') from None
            raise RSIClientError('submission_outcome_unknown' if method=='POST' else 'query_unavailable') from None
        finally:
            timer.cancel()
            if response is not None:response.close()
            connection.close()

    def capabilities(self):
        value=self._request('GET','/v1/capabilities')
        if value.get('schema_version')!='skill-rsi-capabilities-v1':raise RSIClientError('invalid_response_shape')
        if value.get('executor_precondition_supported') is not True:
            raise RSIClientError('executor_precondition_unsupported')
        identity=_digest(value.get('executor_id'))
        if self.executor_id is not None and identity!=self.executor_id:raise RSIClientError('executor_identity_changed')
        self.executor_id=identity
        self._capabilities_seen=True
        return value

    def _job(self, value, identity):
        if (value.get('request_id')!=identity or not isinstance(value.get('state'),str) or value['state'] not in STATES
            or 'result' not in value
            or value.get('result') is not None and not isinstance(value['result'],dict)):
            raise RSIClientError('evaluation_response_binding_invalid')
        executor=_digest(value.get('executor_id'))
        if self.executor_id is not None and executor!=self.executor_id:
            raise RSIClientError('executor_identity_changed')
        if value['state']=='completed':
            result=value['result']
            if (not isinstance(result,dict) or result.get('schema_version')!='skill-rsi-result-v1'
                or result.get('request_id')!=identity or result.get('executor_id')!=executor
                or result.get('training_eligible') is not False):
                raise RSIClientError('evaluation_response_binding_invalid')
        self.executor_id=executor
        return value

    def submit(self, request):
        if not isinstance(request,dict):raise RSIClientError('invalid_request_json')
        request=_snapshot(request)
        identity=_uuid(request.get('request_id'))
        if not self._capabilities_seen:self.capabilities()
        return self._job(self._request('POST','/v1/evaluations',request,expected=(200,202),pin=True),identity)

    def get_evaluation(self, identity):
        identity=_uuid(identity)
        return self._job(self._request('GET','/v1/evaluations/'+identity),identity)

    def wait(self, identity, *, timeout_seconds=900, poll_seconds=1):
        """GET polling only. A timeout neither cancels nor resubmits the task."""
        identity=_uuid(identity)
        deadline=time.monotonic()+_duration(timeout_seconds,3600)
        interval=_duration(poll_seconds,30)
        while True:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise RSIClientError('evaluation_wait_timeout')
            try:
                job=self._job(self._request('GET','/v1/evaluations/'+identity,
                    timeout=min(self.timeout_seconds,remaining)),identity)
            except RSIClientError as error:
                if error.code=='query_timeout' and remaining<=self.timeout_seconds:
                    raise RSIClientError('evaluation_wait_timeout') from None
                raise
            if job['state'] not in ('accepted','running'):return job
            time.sleep(min(interval,max(0,deadline-time.monotonic())))

    def tool_catalog(self, task_id):
        value=self._request('GET','/v1/tool-catalogs/'+_component(task_id))
        if value.get('schema_version')!='skill-rsi-tool-catalog-v1' or value.get('task_id')!=task_id:
            raise RSIClientError('invalid_response_shape')
        return value

    def current_head(self, skill_id):
        value=self._request('GET','/v1/skill-heads/'+_component(skill_id))
        if (value.get('schema_version')!='skill-rsi-head-v1' or value.get('namespace')!='diagnostic'
            or value.get('skill_id')!=skill_id or 'version' not in value or value.get('training_eligible') is not False):
            raise RSIClientError('invalid_response_shape')
        if value.get('version') is not None:_digest(value['version'])
        return value

    def register(self, request):
        request=_snapshot(request)
        try:version=skill_version(request['skill'])
        except (RSIError,KeyError,TypeError):raise RSIClientError('invalid_request_json') from None
        return self._version(self._request('POST','/v1/versions',request),version)

    def version(self, version):
        return self._version(self._request('GET','/v1/versions/'+_digest(version)),version)

    @staticmethod
    def _version(value, expected):
        if (value.get('schema_version')!='skill-rsi-version-v1' or value.get('namespace')!='diagnostic'
            or value.get('version')!=expected or value.get('training_eligible') is not False
            or type(value.get('is_current_head')) is not bool):
            raise RSIClientError('invalid_response_shape')
        return value
