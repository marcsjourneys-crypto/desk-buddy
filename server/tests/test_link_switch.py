from desk_buddy.link.switch import DeviceSwitch
from desk_buddy.loop import State


class Mic:
    def __init__(self, pcm):
        self.pcm, self.started, self.stopped = pcm, 0, 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1
        return self.pcm


class Player:
    def __init__(self):
        self.gen, self.played, self.stops = 0, [], 0

    def current_token(self):
        return self.gen

    def play(self, pcm, token):
        if token == self.gen:
            self.played.append(pcm)

    def stop(self):
        self.stops += 1
        self.gen += 1


class Robot:
    def __init__(self):
        self.mic, self.player, self.sent = Mic(b"robot"), Player(), []

    def send(self, item):
        self.sent.append(item)


def make():
    pc_mic, pc_speaker = Mic(b"pc"), Player()
    return DeviceSwitch(pc_mic, pc_speaker), pc_mic, pc_speaker


def test_pc_devices_when_no_robot():
    switch, pc_mic, pc_speaker = make()
    switch.mic.start()
    assert pc_mic.started == 1
    assert switch.mic.stop() == b"pc"
    assert pc_mic.stopped == 1
    switch.player.play(b"a", switch.player.current_token())
    assert pc_speaker.played == [b"a"]


def test_robot_takes_over_and_gives_back():
    switch, pc_mic, pc_speaker = make()
    robot = Robot()
    assert switch.attach(robot) is None
    assert switch.robot is robot
    switch.mic.start()
    assert robot.mic.started == 1 and pc_mic.started == 0
    assert switch.mic.stop() == b"robot"
    assert pc_mic.stopped == 1  # both stopped, so the PC mic can't be left recording
    switch.player.play(b"a", switch.player.current_token())
    assert robot.player.played == [b"a"] and pc_speaker.played == []
    assert switch.detach(robot) is True
    assert switch.robot is None
    switch.player.play(b"b", switch.player.current_token())
    assert pc_speaker.played == [b"b"]


def test_attach_returns_the_replaced_robot_and_stale_detach_is_ignored():
    switch, _, _ = make()
    old, new = Robot(), Robot()
    switch.attach(old)
    assert switch.attach(new) is old
    assert switch.detach(old) is False
    assert switch.robot is new


def test_stop_invalidates_tokens_and_stops_every_device():
    switch, _, pc_speaker = make()
    robot = Robot()
    switch.attach(robot)
    token = switch.player.current_token()
    switch.player.stop()
    switch.player.play(b"late", token)
    assert robot.player.played == [] and pc_speaker.played == []
    assert robot.player.stops == 1 and pc_speaker.stops == 1


def test_stop_without_a_robot_stops_the_pc_speaker():
    switch, _, pc_speaker = make()
    switch.player.stop()
    assert pc_speaker.stops == 1
    assert switch.player.current_token() == 1


def test_play_uses_the_device_token_not_the_switch_token():
    switch, _, pc_speaker = make()
    pc_speaker.gen = 7  # the device's own generation differs from the switch's
    switch.player.play(b"a", switch.player.current_token())
    assert pc_speaker.played == [b"a"]


def test_state_and_emotion_go_to_the_robot_only_when_attached():
    switch, _, _ = make()
    switch.send_state(State.IDLE)  # no robot: nothing happens
    switch.send_emotion("happy")
    robot = Robot()
    switch.attach(robot)
    switch.send_state(State.THINKING)
    switch.send_emotion("happy")
    assert robot.sent == [
        {"type": "state", "name": "thinking"},
        {"type": "emotion", "name": "happy"},
    ]
