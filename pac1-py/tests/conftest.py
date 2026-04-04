"""Test configuration — mock heavy dependencies so unit tests run without gRPC/API libs."""
import sys
import types
from unittest.mock import MagicMock

# Stub out heavy external modules before any agent imports
_STUB_MODULES = [
    "google", "google.protobuf", "google.protobuf.json_format",
    "connectrpc", "connectrpc.errors",
    "anthropic", "openai", "pydantic", "annotated_types",
    "bitgn", "bitgn.vm", "bitgn.vm.pcm_connect", "bitgn.vm.pcm_pb2",
]

for mod_name in _STUB_MODULES:
    if mod_name not in sys.modules:
        sys.modules[mod_name] = MagicMock()

# Provide annotated_types stubs
_at = sys.modules["annotated_types"]
_at.Ge = lambda x: None
_at.Le = lambda x: None
_at.MaxLen = lambda x: None
_at.MinLen = lambda x: None

# Provide minimal Pydantic BaseModel so models.py can define its classes
_pydantic = sys.modules["pydantic"]
_pydantic.BaseModel = type("BaseModel", (), {"__init_subclass__": classmethod(lambda cls, **kw: None)})
_pydantic.Field = lambda *a, **kw: None
_pydantic.field_validator = lambda *a, **kw: lambda fn: fn

# Provide Outcome enum stub
_pcm_pb2 = sys.modules["bitgn.vm.pcm_pb2"]
_pcm_pb2.Outcome = types.SimpleNamespace(
    OUTCOME_OK="OUTCOME_OK",
    OUTCOME_DENIED_SECURITY="OUTCOME_DENIED_SECURITY",
    OUTCOME_NONE_CLARIFICATION="OUTCOME_NONE_CLARIFICATION",
    OUTCOME_NONE_UNSUPPORTED="OUTCOME_NONE_UNSUPPORTED",
    OUTCOME_ERR_INTERNAL="OUTCOME_ERR_INTERNAL",
)
_pcm_pb2.AnswerRequest = MagicMock
_pcm_pb2.ListRequest = MagicMock
_pcm_pb2.ReadRequest = MagicMock

# Provide ValidationError stub
_pydantic.ValidationError = type("ValidationError", (Exception,), {})

# Provide MessageToDict stub
sys.modules["google.protobuf.json_format"].MessageToDict = lambda x: {}

# Provide ConnectError stub
sys.modules["connectrpc.errors"].ConnectError = type("ConnectError", (Exception,), {})
