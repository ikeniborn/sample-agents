from bitgn._connect import ConnectClient
from bitgn.vm.pcm_pb2 import (
    TreeRequest, TreeResponse,
    FindRequest, FindResponse,
    SearchRequest, SearchResponse,
    ListRequest, ListResponse,
    ReadRequest, ReadResponse,
    WriteRequest, WriteResponse,
    DeleteRequest, DeleteResponse,
    MkDirRequest, MkDirResponse,
    MoveRequest, MoveResponse,
    AnswerRequest, AnswerResponse,
    ContextRequest, ContextResponse,
)

_SERVICE = "bitgn.vm.pcm.PcmRuntime"


class PcmRuntimeClientSync:
    def __init__(self, base_url: str):
        self._c = ConnectClient(base_url)

    def tree(self, req: TreeRequest) -> TreeResponse:
        return self._c.call(_SERVICE, "Tree", req, TreeResponse)

    def find(self, req: FindRequest) -> FindResponse:
        return self._c.call(_SERVICE, "Find", req, FindResponse)

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

    def mk_dir(self, req: MkDirRequest) -> MkDirResponse:
        return self._c.call(_SERVICE, "MkDir", req, MkDirResponse)

    def move(self, req: MoveRequest) -> MoveResponse:
        return self._c.call(_SERVICE, "Move", req, MoveResponse)

    def answer(self, req: AnswerRequest) -> AnswerResponse:
        return self._c.call(_SERVICE, "Answer", req, AnswerResponse)

    def context(self, req: ContextRequest) -> ContextResponse:
        return self._c.call(_SERVICE, "Context", req, ContextResponse)
