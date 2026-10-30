from zoomollo.patterns import guess_emails, infer_pattern, split_name


def test_split_name():
    assert split_name("José  María Núñez") == ("jose", "nunez")
    assert split_name("Cher") is None


def test_infer_and_guess():
    ex = [("Jane Doe", "jane.doe@acme.com"), ("Bob Smith", "bob.smith@acme.com"), ("Al Wu", "awu@acme.com")]
    pattern, conf = infer_pattern(ex)
    assert pattern == "{first}.{last}" and round(conf, 2) == 0.67
    assert guess_emails("Sam Lee", "acme.com", "{f}{last}")[0] == "slee@acme.com"
    assert guess_emails("Sam Lee", "acme.com")[0] == "sam.lee@acme.com"
