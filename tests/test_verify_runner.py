from saas_bench.verify_runner import build_verify_env, _parse_verify_output


def test_parse_verify_output_preserves_multiline_detail():
    output = """[FAIL] (2pt) 2. status  (judge error: upstream returned
choices[0] has no message)
SCORE: 0.000  PASS: False  (0/2)
"""

    parsed = _parse_verify_output(output)

    assert parsed["checks"] == [{
        "label": "2. status",
        "weight": 2,
        "passed": False,
        "detail": "judge error: upstream returned\nchoices[0] has no message",
    }]
    assert parsed["score"] == 0.0
    assert parsed["earned"] == 0
    assert parsed["total"] == 2


def test_parse_verify_output_keeps_single_line_checks_separate():
    output = """diagnostic before checks
[PASS] (1pt) first check
[FAIL] (2pt) second check  (missing value)
SCORE: 0.333  PASS: False  (1/3)
"""

    parsed = _parse_verify_output(output)

    assert [check["label"] for check in parsed["checks"]] == [
        "first check",
        "second check",
    ]
    assert parsed["checks"][1]["detail"] == "missing value"
    assert parsed["score"] == 0.333


def test_build_verify_env_derives_generic_site_contract():
    task = {
        "meta": {
            "meta_data": {
                "sites": ["fake-site"],
            },
        },
    }

    env = build_verify_env(
        task,
        slot_id=3,
        port_map={"fake-site": 32123},
        hostname="127.0.0.1",
    )

    assert env["SERVER_HOSTNAME"] == "127.0.0.1"
    assert env["FAKE_SITE_PORT"] == "32123"
    assert env["FAKE_SITE_CONTAINER"].endswith("_3_fake-site")
