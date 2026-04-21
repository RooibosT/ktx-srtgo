from __future__ import annotations

import os

from ktxgo import cli


def test_prompt_render_uses_terminal_height_not_width() -> None:
    render = cli._KTXConsoleRender()
    render.terminal = type(
        "DummyTerminal",
        (),
        {
            "width": 120,
            "height": 8,
        },
    )()

    assert render.height == 8


def test_prompt_option_window_shrinks_for_small_terminal(monkeypatch) -> None:
    import inquirer.render.console._checkbox as checkbox_render
    import inquirer.render.console._list as list_render

    original_list_max = list_render.MAX_OPTIONS_DISPLAYED_AT_ONCE
    original_checkbox_max = checkbox_render.MAX_OPTIONS_DISPLAYED_AT_ONCE

    monkeypatch.setattr(
        cli.shutil,
        "get_terminal_size",
        lambda fallback: os.terminal_size((80, 8)),
    )

    with cli._inquirer_prompt_render_context():
        assert list_render.MAX_OPTIONS_DISPLAYED_AT_ONCE == 3
        assert list_render.half_options == 1
        assert checkbox_render.MAX_OPTIONS_DISPLAYED_AT_ONCE == 3
        assert checkbox_render.half_options == 1

    assert list_render.MAX_OPTIONS_DISPLAYED_AT_ONCE == original_list_max
    assert checkbox_render.MAX_OPTIONS_DISPLAYED_AT_ONCE == original_checkbox_max


def test_list_input_guarded_uses_safe_prompt_render(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_list_input(**kwargs):
        captured.update(kwargs)
        return "reserve"

    monkeypatch.setattr(cli.inquirer, "list_input", fake_list_input)

    assert cli._list_input_guarded(message="메뉴", choices=["예매"]) == "reserve"
    assert isinstance(captured["render"], cli._KTXConsoleRender)


def test_prompt_render_truncates_options_to_terminal_width(monkeypatch) -> None:
    render = cli._KTXConsoleRender()
    render.terminal = type(
        "DummyTerminal",
        (),
        {
            "width": 20,
            "height": 24,
        },
    )()
    printed: list[str] = []
    monkeypatch.setattr(
        render,
        "print_line",
        lambda _base, **kwargs: printed.append(str(kwargs["m"])),
    )

    option_render = type(
        "DummyOptionRender",
        (),
        {
            "get_options": lambda self: [
                ("예약대기 SMS 알림 번호 등록/수정" * 3, ">", ""),
            ],
        },
    )()

    render._print_options(option_render)

    assert printed
    assert cli._display_width(printed[0]) <= 17
