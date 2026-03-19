from bitgn._connect import ConnectClient
from bitgn.harness_pb2 import (
    StatusRequest, StatusResponse,
    GetBenchmarkRequest, GetBenchmarkResponse,
    StartPlaygroundRequest, StartPlaygroundResponse,
    EndTrialRequest, EndTrialResponse,
)

_SERVICE = "bitgn.harness.HarnessService"


class HarnessServiceClientSync:
    def __init__(self, base_url: str):
        self._c = ConnectClient(base_url)

    def status(self, req: StatusRequest) -> StatusResponse:
        return self._c.call(_SERVICE, "Status", req, StatusResponse)

    def get_benchmark(self, req: GetBenchmarkRequest) -> GetBenchmarkResponse:
        return self._c.call(_SERVICE, "GetBenchmark", req, GetBenchmarkResponse)

    def start_playground(self, req: StartPlaygroundRequest) -> StartPlaygroundResponse:
        return self._c.call(_SERVICE, "StartPlayground", req, StartPlaygroundResponse)

    def end_trial(self, req: EndTrialRequest) -> EndTrialResponse:
        return self._c.call(_SERVICE, "EndTrial", req, EndTrialResponse)
