from uva.replies import PAGE_CHARS, Reply, paginate


def test_short_text_is_one_page():
    assert paginate("oi\ntudo bem") == ["oi\ntudo bem"]
    assert Reply.paginated("oi").pages == []


def test_pages_respect_size_lines_and_header():
    text = "\n".join("x" * 100 for _ in range(100))
    pages = paginate(text, header="HEAD")
    assert len(pages) > 1
    assert all(p.startswith("HEAD\n\n") and len(p) <= PAGE_CHARS for p in pages)
    assert sum(p.count("x" * 100) for p in pages) == 100  # nothing lost


def test_huge_single_line_is_wrapped():
    pages = paginate("y" * (PAGE_CHARS * 2 + 10))
    assert len(pages) == 3 and "".join(pages) == "y" * (PAGE_CHARS * 2 + 10)


def test_long_reply_becomes_a_file_in_english():
    from uva import i18n

    i18n.set_language("en")
    fitted = Reply(text="z" * 5000).fit_for_telegram()
    assert fitted.text == "The reply got long, here it is as a file."
    assert fitted.document == ("z" * 5000).encode()
