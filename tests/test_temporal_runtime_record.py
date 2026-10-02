from ecarsi.control.temporal import runtime_record, same_runtime


def test_moving_the_binaries_keeps_the_runtime_and_an_upgrade_does_not(tmp_path):
    def install(root, server=b"server 1.32"):
        (root / "schema/temporal/versioned/v1.0").mkdir(parents=True)
        (root / "schema/temporal/versioned/v1.0/schema.sql").write_text("create table t();")
        for name, body in (("postgres", b"pg 16"), ("temporal-server", server)):
            (root / name).write_bytes(body)
        return runtime_record([root / "postgres", root / "temporal-server"], root / "schema")
    old, moved = install(tmp_path / "scratch"), install(tmp_path / "image")
    assert old == moved and set(old) == {"postgres", "temporal-server", "schema/temporal/versioned/v1.0/schema.sql"}
    legacy = {str(tmp_path / "scratch" / k): v for k, v in old.items()}    # pre-2026-10-01 records were keyed by path
    assert same_runtime(legacy, moved)
    assert not same_runtime(old, install(tmp_path / "upgraded", server=b"server 1.33"))
