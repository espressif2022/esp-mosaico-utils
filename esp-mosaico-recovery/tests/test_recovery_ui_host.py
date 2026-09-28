"""Exercise the production GSP scenes and C controller through native input."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest
from PIL import Image
import zxingcpp

ROOT = Path(__file__).resolve().parents[1]
RECOVERY = ROOT / "firmware/recovery"


class Simulator:
    def __init__(self, directory, executable, bundle, host, scenario=""):
        self.directory = directory
        self.state_file = directory / "state.json"
        self.log = (directory / "sim.log").open("w")
        ready = directory / "ready.json"
        self.sim = subprocess.Popen([
            str(host), "--bundle", str(bundle), "--headless", "--backend-enable",
            "--backend-required", "--api-listen", "tcp://127.0.0.1:0",
            "--ready-file", str(ready)], stdout=self.log, stderr=self.log)
        self.backend = None
        self.connection = None
        self.stream = None
        self.sequence = 0
        try:
            deadline = time.monotonic() + 20
            while not ready.exists():
                assert self.sim.poll() is None, "simulator exited before ready"
                assert time.monotonic() < deadline, "simulator startup timed out"
                time.sleep(.05)
            info = json.loads(ready.read_text())
            self.backend = subprocess.Popen([str(executable), "--endpoint", info["backend"]],
                env=dict(os.environ, VIBE_SIM_STATE_FILE=str(self.state_file),
                    VIBE_SIM_CONTROL_FILE=str(directory / "control"), VIBE_SIM_SCENARIO=scenario),
                stdout=self.log, stderr=self.log)
            address, port = info["api"].replace("tcp://", "").split(":")
            self.connection = socket.create_connection((address, int(port)), timeout=15)
            self.stream = self.connection.makefile("rb")
            self.wait(page=0)
        except BaseException:
            self.close()
            raise

    def rpc(self, method, **params):
        self.sequence += 1
        body = json.dumps(dict(jsonrpc="2.0", id=self.sequence, method=method, params=params)).encode()
        self.connection.sendall(("Content-Length: %d\r\n\r\n" % len(body)).encode() + body)
        while True:
            headers = {}
            while True:
                line = self.stream.readline()
                assert line, "simulator disconnected"
                if line in (b"\r\n", b"\n"):
                    break
                key, value = line.decode().split(":", 1)
                headers[key.lower()] = value.strip()
            result = json.loads(self.stream.read(int(headers["content-length"])))
            if result.get("id") == self.sequence:
                assert "error" not in result, result
                return result.get("result")

    def wait(self, *, timeout=15, **expected):
        deadline = time.monotonic() + timeout
        actual = {}
        while time.monotonic() < deadline:
            assert self.backend.poll() is None, (self.directory / "sim.log").read_text()
            try:
                snapshot = self.state_file.read_text(encoding="utf-8")
            except (FileNotFoundError, PermissionError):
                # Windows briefly denies reads while MoveFileEx replaces the
                # snapshot. Retry within the same deadline, never reset it.
                pass
            else:
                actual = json.loads(snapshot)
                if all(actual.get(k) == v for k, v in expected.items()):
                    # Let already-enqueued visibility and list updates paint.
                    self.rpc("wait", frames=20)
                    return actual
            time.sleep(.05)
        pytest.fail("expected %r; observed %r; %s" % (expected, actual, (self.directory / "sim.log").read_text()))

    def tap(self, x, y):
        self.rpc("tap", x=x, y=y)
        self.rpc("wait", frames=8)

    def capture(self, name):
        self.rpc("screenshot", path=str(self.directory / (name + ".png")), format="png")

    def open_bridge(self):
        # Same public controller entry used by the device's queued USB RPC.
        self.control("open-bridge")

    def control(self, command):
        request = self.directory / "control"
        temporary = self.directory / "control.tmp"
        temporary.write_text(command)
        temporary.replace(request)
        deadline = time.monotonic() + 5
        while request.exists():
            assert time.monotonic() < deadline
            time.sleep(.05)
        self.rpc("wait", frames=20)

    def close(self):
        if self.stream:
            self.stream.close()
        if self.connection:
            self.connection.close()
        # Stop backend first: deinit can still communicate with the renderer.
        for process in (self.backend, self.sim):
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        self.log.close()


@pytest.fixture(scope="module")
def native_build(tmp_path_factory):
    component = Path(os.environ.get("ESP_GSP_COMPONENT_DIR", RECOVERY / "managed_components/espressif__esp-gsp"))
    assert (component / "tools/sim_bridge/build.py").is_file(), "Build Recovery first or set ESP_GSP_COMPONENT_DIR"
    build = tmp_path_factory.mktemp("vibe-native-build")
    gspc = os.environ.get("GSPC_EXECUTABLE")
    host = os.environ.get("GSP_SIM_EXECUTABLE")
    assert gspc and host, "Set GSPC_EXECUTABLE and GSP_SIM_EXECUTABLE to the pinned GSP tools"
    build_python = os.environ.get("GSP_BUILD_PYTHON", sys.executable)
    result = subprocess.run([build_python, str(component / "tools/sim_bridge/build.py"),
        "--project", str(RECOVERY / "pc"), "--build-dir", str(build),
        "--component-dir", str(component), "--gspc", gspc], capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads((build / "vibe_backend-Release.json").read_text())
    return Path(manifest["executable"]), Path(manifest["bundle"]), Path(host)


@pytest.fixture
def sim(tmp_path, native_build, request):
    instance = Simulator(tmp_path, *native_build, scenario=getattr(request, "param", ""))
    try:
        yield instance
    finally:
        instance.close()


def test_keyboard_limits_masking_and_cancel(sim):
    sim.capture("home")
    sim.tap(240, 390)
    sim.wait(page=1, pending=True)
    sim.capture("wifi")
    sim.tap(180, 251)
    sim.wait(page=2, password_length=0, password_visible=False)
    sim.capture("keyboard")
    for _ in range(65):
        sim.tap(35, 258)  # native q key; the 65th character must be rejected
    sim.wait(password_length=64)
    sim.capture("password-masked")
    sim.tap(431, 376)  # Backspace
    sim.wait(password_length=63)
    sim.tap(414, 146)
    sim.wait(password_visible=True)
    sim.tap(38, 36)
    sim.wait(page=1, password_length=0, password_visible=False, pending=True)
    sim.tap(38, 36)
    sim.wait(page=0, pending=False, bridge_running=False)


def test_download_wifi_continuation_and_cancel(sim):
    sim.tap(240, 390)
    sim.wait(page=1, pending=True)
    sim.tap(180, 251)
    sim.wait(page=2)
    for _ in range(8):
        sim.tap(35, 258)
    sim.wait(password_length=8)
    sim.tap(420, 435)  # GO
    sim.wait(page=4, pending=False, network=2, bridge_running=True, password_length=0)
    sim.capture("download-ideas")
    sim.wait(bridge_open_calls=1)
    sim.open_bridge()
    sim.open_bridge()
    sim.wait(page=4, bridge_running=True, bridge_open_calls=1)
    sim.tap(240, 444)
    sim.wait(page=0, bridge_running=False)
    sim.tap(75, 445)  # ordinary Wi-Fi entry never starts a download
    sim.wait(page=1, pending=False, bridge_running=False)


@pytest.mark.parametrize("sim", ["prefetched"], indirect=True)
def test_prefetched_code_reused_after_back_and_explicit_cancel(sim):
    sim.wait(page=0, bridge_prefetch_calls=1, bridge_open_calls=0,
             bridge_running=True, bridge_active=False, code_ready=True)
    sim.tap(240, 390)
    sim.wait(page=4, bridge_active=True, code_ready=True, bridge_open_calls=1)
    sim.capture("prefetched-code")
    sim.tap(38, 36)
    sim.wait(page=0, bridge_running=True, bridge_active=False, code_ready=True)
    sim.tap(240, 390)
    sim.wait(page=4, bridge_active=True, bridge_open_calls=2, bridge_prefetch_calls=1)
    sim.tap(240, 444)
    sim.wait(page=0, bridge_running=False, bridge_active=False, code_ready=False)
    sim.tap(240, 390)
    sim.wait(page=4, bridge_running=True, bridge_open_calls=3, bridge_prefetch_calls=1)


def test_wifi_cards_and_labels_survive_navigation_and_rescan(sim):
    def check_frame(name):
        sim.capture(name)
        path = sim.directory / (name + ".ppm")
        sim.rpc("screenshot", path=str(path), format="ppm")
        with path.open("rb") as frame:
            assert frame.readline().strip() == b"P6"
            assert frame.readline().split() == [b"480", b"480"]
            assert frame.readline().strip() == b"255"
            pixels = frame.read()
        assert len(pixels) == 480 * 480 * 3

        def region(x1, y1, x2, y2):
            return [pixels[(y * 480 + x) * 3:(y * 480 + x) * 3 + 3]
                    for y in range(y1, y2) for x in range(x1, x2)]

        # Actual rendered glyphs, not just successful visibility callbacks.
        for box, minimum in [((190, 12, 290, 60), 200),  # Wi-Fi
                             ((20, 20, 50, 50), 5),    # Back
                             ((360, 102, 438, 145), 200),  # Forget
                             ((363, 176, 450, 208), 200)]:  # Rescan
            assert sum(min(rgb) > 160 for rgb in region(*box)) > minimum, name
        # Three complete cards, separated from each other and the footer.
        for y in (288, 360, 434):
            assert all(max(rgb) == 0 for rgb in region(48, y, 430, y + 2)), name
        for y in (280, 352, 424):
            assert all(min(rgb) >= 24 for rgb in region(80, y, 380, y + 2)), name

    for cycle in range(3):
        sim.tap(75, 445)
        sim.wait(page=1)
        check_frame("wifi-entry-%d" % cycle)
        sim.tap(180, 251)
        sim.wait(page=2)
        sim.tap(38, 36)
        sim.wait(page=1)
        sim.tap(410, 190)
        sim.wait(page=1)
        check_frame("wifi-rescan-%d" % cycle)
        sim.tap(38, 36)
        sim.wait(page=0)


def test_nand_update_and_result_acknowledgement(sim):
    sim.tap(370, 446)
    sim.wait(page=5)
    sim.capture("nand")
    sim.tap(150, 170)
    sim.wait(page=6)
    sim.capture("confirm")
    sim.tap(350, 430)
    sim.wait(page=7)
    sim.capture("update")
    # Twenty asynchronous service ticks each issue renderer RPCs. macOS CI
    # can exceed 15 wall-clock seconds while still advancing normally.
    sim.wait(timeout=45, page=8, progress=1000)
    sim.capture("result")
    sim.tap(240, 409)
    sim.wait(page=0)
    time.sleep(.6)
    sim.wait(page=0)  # an acknowledged terminal result must not reopen


@pytest.mark.parametrize("sim", ["wifi-fail-once"], indirect=True)
def test_network_failure_and_retry_preserve_download_intent(sim):
    sim.tap(240, 390)
    sim.wait(page=1, pending=True)
    for expected_network in (3, 2):
        sim.tap(180, 251)
        sim.wait(page=2, password_length=0)
        for _ in range(8):
            sim.tap(35, 258)
        sim.tap(420, 435)
        if expected_network == 3:
            sim.wait(page=1, network=3, pending=True, password_length=0)
            sim.capture("network-failure")
        else:
            sim.wait(page=4, network=2, pending=False, bridge_running=True)


@pytest.mark.parametrize("sim", ["update-fail"], indirect=True)
def test_update_failure_returns_home_without_reopening(sim):
    sim.tap(370, 446)
    sim.wait(page=5)
    sim.tap(150, 170)
    sim.wait(page=6)
    sim.tap(350, 430)
    sim.wait(page=8, progress=500)
    sim.capture("update-failure")
    sim.tap(240, 409)
    sim.wait(page=0)
    time.sleep(.6)
    sim.wait(page=0)


@pytest.mark.parametrize("sim", ["empty"], indirect=True)
def test_empty_lists_remain_navigable(sim):
    sim.tap(75, 445)
    sim.wait(page=1, pending=False)
    sim.capture("wifi-empty")
    sim.tap(180, 251)
    sim.wait(page=1)
    sim.tap(38, 36)
    sim.wait(page=0)
    sim.tap(370, 446)
    sim.wait(page=5)
    sim.capture("nand-empty")
    sim.tap(150, 170)
    sim.wait(page=5)
    sim.tap(38, 36)
    sim.wait(page=0)


@pytest.mark.parametrize("sim", ["prefetched", "long-code", "waiting-code"], indirect=True)
def test_download_qr_decodes_from_native_frame_and_code_survives_navigation(sim):
    sim.tap(240, 390)
    sim.wait(page=4)
    sim.capture("ideas-qr")
    frame = Image.open(sim.directory / "ideas-qr.png")
    codes = zxingcpp.read_barcodes(frame)
    assert [code.text for code in codes] == ["https://mosaico-ideas.espressif.com/"]
    snapshot = sim.wait(page=4)
    if snapshot["code_length"] == 15:
        # Worst-case wide characters must have visible ink and side margins.
        assert frame.crop((40, 328, 440, 362)).getbbox()
        pixels = frame.convert("RGB")
        for x in (36, 443):
            assert all(max(pixels.getpixel((x, y))) < 100 for y in range(321, 372))
    sim.tap(38, 36)
    sim.wait(page=0, bridge_running=True, bridge_active=False)
    sim.tap(240, 390)
    sim.wait(page=4, bridge_running=True, bridge_active=True)
    sim.control("expire-code")
    sim.wait(renewed_code=True)
    sim.tap(240, 444)
    sim.wait(page=0, bridge_running=False, code_ready=False)


def test_ota_telemetry_component_transition_stall_retry_and_terminal_states(sim):
    def sample(ms, received, total=4*1024*1024, component=1, completed=0,
               receiving=1, job=1, component_received=None, terminal=0):
        if component_received is None:
            component_received = received
        sim.control("transfer %d %d %d %d 2 %d %d %d %d 2097152 %d" %
                    (ms, received, total, component, completed, receiving, job, component_received, terminal))
        return sim.wait(sample_ms=ms)

    sample(0, 0)
    sim.wait(page=7, progress=0, rate_valid=False)
    sample(1000, 1024*1024)
    sim.wait(progress=250, rate_valid=True, rate_bps=1024*1024)
    sim.capture("ota-downloading")
    sample(2000, 1024*1024)
    sim.wait(progress=250, rate_bps=0)
    sim.capture("ota-stalled")
    sample(2250, 2*1024*1024, receiving=0)
    sim.wait(progress=500, rate_valid=False)
    sim.capture("ota-component-verified")
    sample(2500, 2*1024*1024, component=2, completed=1, component_received=0)
    sim.wait(progress=500, rate_valid=False, component_id=2)
    sample(3500, 3*1024*1024, component=2, completed=1, component_received=1024*1024)
    sim.wait(progress=750, rate_valid=True, rate_bps=1024*1024)
    sim.capture("ota-second-component")
    sample(3750, 2*1024*1024, component=2, completed=1, component_received=0)
    sim.wait(progress=500, rate_valid=False)  # retry rolls back only the active payload
    sample(4000, 0, total=0, job=2)
    sim.wait(progress=0, rate_valid=False)
    sim.capture("ota-unknown-size")
    sample(5000, 1024, total=0, job=2)
    sim.wait(rate_valid=True, rate_bps=1024)
    sample(6000, 4*1024*1024, component=0, completed=2, receiving=0, job=3)
    sim.wait(page=7, progress=1000, rate_valid=False)
    sim.capture("ota-committing")
    sample(7000, 4*1024*1024, component=0, completed=2, receiving=0, job=3, terminal=2)
    sim.wait(page=8, progress=1000, rate_valid=False)
    sim.capture("ota-failed-after-transfer")
    sim.tap(240, 409)
    sim.wait(page=0)
