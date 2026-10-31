from zoomollo.extract import extract_page, find_emails, is_role_email, normalize_phone

HTML = """<html><head><title>Acme | Team</title>
<script type="application/ld+json">{"@type":"Person","name":"Rita Ruiz","jobTitle":"CFO","email":"rita.ruiz@acme.com","telephone":"+1 415 555 0134"}</script>
</head><body>
<a href="mailto:jane.doe@acme.com">Jane Doe</a>
<a href="mailto:info@acme.com?subject=hi">Email us</a>
<a href="tel:+1-212-555-0198">Call</a>
<p>Reach Bob at bob [at] acme [dot] com or (646) 555-0112.</p>
<img src="logo@2x.png"> noreply@acme.com user@example.com
<a href="https://www.linkedin.com/company/acme/">LinkedIn</a>
<a href="/careers">Careers</a>
</body></html>"""


def test_extract_page():
    r = extract_page(HTML, "https://acme.com/team")
    assert r.emails == {"jane.doe@acme.com", "info@acme.com", "bob@acme.com", "rita.ruiz@acme.com"}
    assert {"+12125550198", "+16465550112", "+14155550134"} <= r.phones
    names = {p.name: p for p in r.people}
    assert names["Jane Doe"].email == "jane.doe@acme.com"
    assert names["Rita Ruiz"].title == "CFO"
    assert "https://www.linkedin.com/company/acme" in r.socials
    assert "https://acme.com/careers" in r.links
    assert r.title == "Acme | Team"


def test_junk_filtered():
    assert find_emails("a1b2c3d4e5f6a7b8c9d0@sentry.io x@example.com img@2x.png") == set()


def test_role_and_phone():
    assert is_role_email("careers@acme.com") and not is_role_email("jane@acme.com")
    assert normalize_phone("020 7946 0958", "GB") == "+442079460958"
    assert normalize_phone("12345") is None
