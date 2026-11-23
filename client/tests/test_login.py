"""The device login's link: a terminal hyperlink, and opened in the browser only on a local screen."""


def test_the_link_opens_in_the_browser_only_on_a_local_screen(monkeypatch):
    from agentygg import login

    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url, new=0: opened.append(url) or True)
    monkeypatch.delenv("YGG_NO_BROWSER", raising=False)
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setattr(login.sys, "platform", "darwin")
    assert login.open_browser("https://agentygg.com/device?code=AB-12")
    assert not login.open_browser("http://evil.example/device")  # only https links
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 22 5.6.7.8 22")
    assert not login.open_browser("https://agentygg.com/device?code=AB-12")  # over SSH: no
    monkeypatch.delenv("SSH_CONNECTION")
    monkeypatch.setenv("YGG_NO_BROWSER", "1")
    assert not login.open_browser("https://agentygg.com/device?code=AB-12")
    assert opened == ["https://agentygg.com/device?code=AB-12"]


def test_the_terminal_link_is_a_hyperlink_carrying_the_url():
    from agentygg import login

    link = login.hyperlink("https://agentygg.com/device?code=AB-12")
    assert link.startswith("\033]8;;https://agentygg.com/device?code=AB-12\033\\")
    assert "https://agentygg.com/device?code=AB-12\033]8;;" in link
