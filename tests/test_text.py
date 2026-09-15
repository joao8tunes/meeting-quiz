from quiz import text as tx


def test_clean_removes_control_and_invisible_characters_and_limits_length():
    assert tx.clean("  Ana​ \x00Maria‮  ", 50) == "Ana Maria"
    assert tx.clean("a" * 20, 5) == "aaaaa"
    assert tx.clean("line one\r\n\r\n\r\n\r\nline   two", 100, multiline=True) == "line one\n\nline two"
    assert tx.clean(None, 10) == ""


def test_clean_lines_drops_blanks_and_duplicates_ignoring_case_and_accents():
    assert tx.clean_lines("Mars\n\n  mars \nMárs\nRed planet", 50, 10) == ["Mars", "Red planet"]
    assert tx.clean_lines("\n".join(str(i) for i in range(30)), 50, 3) == ["0", "1", "2"]


def test_normalize_ignores_case_accents_and_punctuation():
    assert tx.normalize("  São Paulo!! ") == tx.normalize("sao-paulo") == "sao paulo"


def test_matches_accepts_exact_answers_and_small_typos_only_when_tolerant():
    assert tx.matches("  MARS. ", ["Mars"], tolerant=False)
    assert tx.matches("Jupyter", ["Jupiter"], tolerant=True)
    assert not tx.matches("Jupyter", ["Jupiter"], tolerant=False)
    assert not tx.matches("Venus", ["Mars"], tolerant=True)
    assert not tx.matches("", ["Mars"], tolerant=True)


def test_matches_never_forgives_typos_in_numbers_or_very_short_answers():
    assert not tx.matches("1946", ["1945"], tolerant=True)
    assert not tx.matches("cat", ["car"], tolerant=True)
    assert tx.matches("1945", ["1945"], tolerant=True)


def test_emails_and_domains():
    assert tx.valid_email("someone@example.com")
    assert not tx.valid_email("someone@example")
    assert not tx.valid_email("some one@example.com")
    assert tx.clean_domains("Example.com, @example.org; not a domain, example.com") == ["example.com", "example.org"]
    assert tx.domain_allowed("a@team.example.com", ["example.com"])
    assert not tx.domain_allowed("a@badexample.com", ["example.com"])
    assert tx.domain_allowed("a@anything.org", [])


def test_escape_markdown_shows_links_and_images_literally():
    escaped = tx.escape_markdown("![x](https://tracker.example/p.png) [click](https://example.com) **bold** :material/home:")
    for token in ("\\!\\[", "\\]\\(", "\\*\\*", "\\:material"):
        assert token in escaped
    assert "](" not in escaped and "**" not in escaped


def test_formatting_helpers():
    assert tx.format_code("123456") == "123 456"
    assert tx.only_digits("12 34-56") == "123456"
    assert tx.format_seconds(12.34) == "12.3 s"
    assert tx.format_seconds(83) == "1 min 23 s"
    assert tx.format_seconds(None) == "—"
    assert tx.format_clock(65) == "01:05"
    assert tx.format_clock(3725) == "1:02:05"
    assert tx.plural(1, "winner") == "1 winner"
    assert tx.plural(2, "winner") == "2 winners"
