"""Unit tests for nativmix.utils.routing loopback helpers."""

from nativmix.utils.routing import (
    build_loopback_load_args,
    loopback_module_targets_hardware,
)


def test_build_loopback_load_args_includes_hardware_sink() -> None:
    args = build_loopback_load_args("NativMix_CH_0.monitor", "alsa_output.usb-TEAC")
    assert args == [
        "module-loopback",
        "source=NativMix_CH_0.monitor",
        "sink=alsa_output.usb-TEAC",
        "dont-link=1",
    ]


def test_loopback_module_targets_hardware_accepts_current_args() -> None:
    argument = (
        "source=NativMix_CH_0.monitor "
        "sink=alsa_output.usb-TEAC_Corporation_US-2x2-00.pro-output-0 "
        "dont-link=1"
    )
    assert loopback_module_targets_hardware(
        argument,
        "NativMix_CH_0",
        "alsa_output.usb-TEAC_Corporation_US-2x2-00.pro-output-0",
    )


def test_loopback_module_targets_hardware_rejects_legacy_module() -> None:
    argument = "source=NativMix_CH_0.monitor dont-link=1"
    assert not loopback_module_targets_hardware(
        argument,
        "NativMix_CH_0",
        "alsa_output.usb-TEAC_Corporation_US-2x2-00.pro-output-0",
    )


def test_loopback_module_targets_hardware_rejects_wrong_sink() -> None:
    argument = (
        "source=NativMix_CH_0.monitor "
        "sink=alsa_output.pci-0000_31_00.4.iec958-stereo dont-link=1"
    )
    assert not loopback_module_targets_hardware(
        argument,
        "NativMix_CH_0",
        "alsa_output.usb-TEAC_Corporation_US-2x2-00.pro-output-0",
    )
