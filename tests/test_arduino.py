from nativmix.hardware.arduino import ArduinoThread


def test_transient_larger_channel_count_is_discarded_without_index_error():
    thread = ArduinoThread(num_channels=2)
    emitted: list[list[float]] = []
    thread.volumes_changed.connect(emitted.append)

    thread._process_line("1|2|3")
    thread._process_line("4|5|6")

    assert thread._num_channels == 2
    assert len(thread._channels) == 2
    assert emitted == []


def test_channel_count_adapts_after_three_stable_clean_frames():
    thread = ArduinoThread(num_channels=2)
    counts: list[int] = []
    thread.channel_count_changed.connect(counts.append)

    thread._process_line("1|2|3")
    thread._process_line("4|5|6")
    thread._process_line("7|8|9")

    assert thread._num_channels == 3
    assert len(thread._channels) == 3
    assert counts == [3]


def test_malformed_channel_count_candidate_does_not_reset_channels():
    thread = ArduinoThread(num_channels=2)

    thread._process_line("1|2|oops")
    thread._process_line("3|4")

    assert thread._num_channels == 2
    assert len(thread._channels) == 2


def test_prepare_for_sleep_blocks_session_until_resume():
    thread = ArduinoThread(num_channels=2)
    assert thread._system_sleeping is False

    thread.prepare_for_sleep()
    assert thread._system_sleeping is True

    # Gate used by run() / _run_session must stay closed
    assert thread._system_sleeping

    thread.resume_from_sleep()
    assert thread._system_sleeping is False


def test_prepare_for_sleep_closes_active_serial_handle():
    thread = ArduinoThread(num_channels=2)

    class _FakeSer:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    fake = _FakeSer()
    thread._active_ser = fake  # type: ignore[assignment]
    thread.prepare_for_sleep()
    assert fake.closed is True
    assert thread._system_sleeping is True
