from bitgn._connect import ConnectClient
from bitgn.vm.mini_pb2 import (
    OutlineRequest, OutlineResponse,
    SearchRequest, SearchResponse,
    ListRequest, ListResponse,
    ReadRequest, ReadResponse,
    WriteRequest, WriteResponse,
    DeleteRequest, DeleteResponse,
    AnswerRequest, AnswerResponse,
)

_SERVICE = "bitgn.vm.mini.MiniRuntime"


class MiniRuntimeClientSync:
    def __init__(self, base_url: str):
        self._c = ConnectClient(base_url)

    def outline(self, req: OutlineRequest) -> OutlineResponse:
        return self._c.call(_SERVICE, "Outline", req, OutlineResponse)

    def search(self, req: SearchRequest) -> SearchResponse:
        return self._c.call(_SERVICE, "Search", req, SearchResponse)

    def list(self, req: ListRequest) -> ListResponse:
        return self._c.call(_SERVICE, "List", req, ListResponse)

    def read(self, req: ReadRequest) -> ReadResponse:
        return self._c.call(_SERVICE, "Read", req, ReadResponse)

    def write(self, req: WriteRequest) -> WriteResponse:
        return self._c.call(_SERVICE, "Write", req, WriteResponse)

    def delete(self, req: DeleteRequest) -> DeleteResponse:
        return self._c.call(_SERVICE, "Delete", req, DeleteResponse)

    def answer(self, req: AnswerRequest) -> AnswerResponse:
        return self._c.call(_SERVICE, "Answer", req, AnswerResponse)
