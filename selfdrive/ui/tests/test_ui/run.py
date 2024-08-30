from collections import namedtuple
import capnp
import pathlib
import shutil
import sys
import jinja2
import matplotlib.pyplot as plt
import numpy as np
import os
import pywinctl
import time

from cereal import messaging, log
from msgq.visionipc import VisionIpcServer, VisionStreamType
from cereal.messaging import SubMaster, PubMaster
from openpilot.common.params import Params
from openpilot.common.transformations.camera import CameraConfig, DEVICE_CAMERAS
from openpilot.selfdrive.test.helpers import with_processes
from openpilot.tools.lib.logreader import LogReader
from openpilot.tools.lib.framereader import FrameReader
from openpilot.tools.lib.route import Route

UI_DELAY = 0.5 # may be slower on CI?
TEST_ROUTE = "a2a0ccea32023010|2023-07-27--13-01-19"

STREAMS: list[tuple[VisionStreamType, CameraConfig, bytes]] = []
DATA: dict[str, capnp.lib.capnp._DynamicStructBuilder] = dict.fromkeys(
  ["deviceState", "pandaStates", "controlsState", "liveCalibration",
  "modelV2", "radarState", "driverMonitoringState",
  "carState", "driverStateV2", "roadCameraState", "wideRoadCameraState"], None)

def setup_common(click, pm: PubMaster):
  Params().put("DongleId", "123456789012345")
  pm.send('deviceState', DATA['deviceState'])

def setup_homescreen(click, pm: PubMaster):
  setup_common(click, pm)

def setup_settings_device(click, pm: PubMaster):
  setup_common(click, pm)

  click(100, 100)

def setup_settings_network(click, pm: PubMaster):
  setup_common(click, pm)

  setup_settings_device(click, pm)
  click(300, 600)

def setup_onroad(click, pm: PubMaster):
  setup_common(click, pm)

  vipc_server = VisionIpcServer("camerad")


  for stream_type, cam, _ in STREAMS:
    vipc_server.create_buffers(stream_type, 5, False, cam.width, cam.height)
  vipc_server.start_listener()

  packet_id = 0
  for _ in range(30):
    for service, data in DATA.items():
      if data:
        data.clear_write_flag()
        pm.send(service, data)

    packet_id = packet_id + 1
    for stream_type, _, image in STREAMS:
      vipc_server.send(stream_type, image, packet_id, packet_id, packet_id)

    time.sleep(0.05)

def setup_onroad_wide(click, pm: PubMaster):
  DATA['controlsState'].controlsState.experimentalMode = True
  DATA["carState"].carState.vEgo = 1
  setup_onroad(click, pm)

def setup_onroad_sidebar(click, pm: PubMaster):
  setup_onroad(click, pm)
  click(500, 500)

def setup_onroad_wide_sidebar(click, pm: PubMaster):
  setup_onroad_wide(click, pm)
  click(500, 500)

def setup_onroad_alert(click, pm: PubMaster, text1, text2, size, status=log.ControlsState.AlertStatus.normal):
  print(f'setup onroad alert, size: {size}')
  setup_onroad(click, pm)
  dat = messaging.new_message('controlsState')
  cs = dat.controlsState
  cs.alertText1 = text1
  cs.alertText2 = text2
  cs.alertSize = size
  cs.alertStatus = status
  cs.alertType = "test_onroad_alert"
  pm.send('controlsState', dat)

def setup_onroad_alert_small(click, pm: PubMaster):
  setup_onroad_alert(click, pm, 'This is a small alert message', '', log.ControlsState.AlertSize.small)

def setup_onroad_alert_mid(click, pm: PubMaster):
  setup_onroad_alert(click, pm, 'Medium Alert', 'This is a medium alert message', log.ControlsState.AlertSize.mid)

def setup_onroad_alert_full(click, pm: PubMaster):
  setup_onroad_alert(click, pm, 'Full Alert', 'This is a full alert message', log.ControlsState.AlertSize.full)

CASES = {
  "homescreen": setup_homescreen,
  "settings_device": setup_settings_device,
  "settings_network": setup_settings_network,
  "onroad": setup_onroad,
  "onroad_sidebar": setup_onroad_sidebar,
  "onroad_wide": setup_onroad_wide,
  "onroad_wide_sidebar": setup_onroad_wide_sidebar,
  "onroad_alert_small": setup_onroad_alert_small,
  "onroad_alert_mid": setup_onroad_alert_mid,
  "onroad_alert_full": setup_onroad_alert_full,
}

TEST_DIR = pathlib.Path(__file__).parent

TEST_OUTPUT_DIR = TEST_DIR / "report_1"
SCREENSHOTS_DIR = TEST_OUTPUT_DIR / "screenshots"


class TestUI:
  def __init__(self):
    os.environ["SCALE"] = "1"
    sys.modules["mouseinfo"] = False

  def setup(self):
    self.sm = SubMaster(["uiDebug"])
    self.pm = PubMaster(list(DATA.keys()))
    while not self.sm.valid["uiDebug"]:
      self.sm.update(1)
    time.sleep(UI_DELAY) # wait a bit more for the UI to start rendering
    try:
      self.ui = pywinctl.getWindowsWithTitle("ui")[0]
    except Exception as e:
      print(f"failed to find ui window, assuming that it's in the top left (for Xvfb) {e}")
      self.ui = namedtuple("bb", ["left", "top", "width", "height"])(0,0,2160,1080)

  def screenshot(self):
    import pyautogui
    im = pyautogui.screenshot(region=(self.ui.left, self.ui.top, self.ui.width, self.ui.height))
    assert im.width == 2160
    assert im.height == 1080
    img = np.array(im)
    im.close()
    return img

  def click(self, x, y, *args, **kwargs):
    import pyautogui
    pyautogui.click(self.ui.left + x, self.ui.top + y, *args, **kwargs)
    time.sleep(UI_DELAY) # give enough time for the UI to react

  @with_processes(["ui"])
  def test_ui(self, name, setup_case):
    self.setup()

    setup_case(self.click, self.pm)

    time.sleep(UI_DELAY) # wait a bit more for the UI to finish rendering

    im = self.screenshot()
    plt.imsave(SCREENSHOTS_DIR / f"{name}.png", im)


def create_html_report():
  OUTPUT_FILE = TEST_OUTPUT_DIR / "index.html"

  with open(TEST_DIR / "template.html") as f:
    template = jinja2.Template(f.read())

  cases = {f.stem: (str(f.relative_to(TEST_OUTPUT_DIR)), "reference.png") for f in SCREENSHOTS_DIR.glob("*.png")}
  cases = dict(sorted(cases.items()))

  with open(OUTPUT_FILE, "w") as f:
    f.write(template.render(cases=cases))

def create_screenshots():
  if TEST_OUTPUT_DIR.exists():
    shutil.rmtree(TEST_OUTPUT_DIR)

  SCREENSHOTS_DIR.mkdir(parents=True)

  route = Route(TEST_ROUTE)

  segnum = 2
  lr = LogReader(route.qlog_paths()[segnum])
  for event in lr:
    if event.which() in DATA:
      DATA[event.which()] = event.as_builder()

    if all(DATA.values()):
      break

  cam = DEVICE_CAMERAS[("tici", "ar0231")]
  road_img = FrameReader(route.camera_paths()[segnum]).get(0, pix_fmt="nv12")[0]
  STREAMS.append((VisionStreamType.VISION_STREAM_ROAD, cam.fcam, road_img.flatten().tobytes()))

  wide_road_img = FrameReader(route.ecamera_paths()[segnum]).get(0, pix_fmt="nv12")[0]
  STREAMS.append((VisionStreamType.VISION_STREAM_WIDE_ROAD, cam.ecam, wide_road_img.flatten().tobytes()))

  t = TestUI()
  for name, setup in CASES.items():
    t.test_ui(name, setup)

if __name__ == "__main__":
  print("creating test screenshots")
  create_screenshots()

  print("creating html report")
  create_html_report()
