from services.keyboard import (
    validate_keyboard_structure,
    build_inline_markup,
    add_row,
    add_button_to_row,
    delete_button,
    move_button,
    reformat_columns,
)


def test_validate_and_build():
    keyboard = [
        [{"text": "A", "url": "https://example.com"}],
        [{"text": "B", "url": "https://t.me/channel/123"}]
    ]
    ok, msg = validate_keyboard_structure(keyboard)
    assert ok
    markup = build_inline_markup(keyboard)
    assert markup.inline_keyboard is not None
    assert len(markup.inline_keyboard) == 2


def test_add_delete_move_reformat():
    kb = []
    kb = add_row(kb)
    kb = add_button_to_row(kb, 0, "Btn1", "https://a.com")
    kb = add_button_to_row(kb, 0, "Btn2", "https://b.com")
    assert len(kb[0]) == 2
    kb = delete_button(kb, 0, 0)
    assert len(kb[0]) == 1
    kb = add_row(kb)
    kb = add_button_to_row(kb, 1, "Btn3", "https://c.com")
    kb = move_button(kb, 0, 0, 1, 1)
    assert isinstance(kb, list)
    kb = reformat_columns(kb, 2)
    assert isinstance(kb, list)