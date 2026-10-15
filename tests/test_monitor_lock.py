def test_monitor_lease_excludes_same_scope_and_releases_lock(tmp_path):
    import pytest

    from sessiongauge.monitor_lock import MonitorAlreadyRunning, MonitorLease

    lease = MonitorLease(data_dir=tmp_path, endpoint="/tmp/fixture-herdr.sock")
    competing_lease = MonitorLease(data_dir=tmp_path, endpoint="/tmp/fixture-herdr.sock")

    with lease, pytest.raises(MonitorAlreadyRunning), competing_lease:
        pass

    with MonitorLease(data_dir=tmp_path, endpoint="/tmp/fixture-herdr.sock"):
        pass


def test_monitor_lease_allows_different_scopes(tmp_path):
    from sessiongauge.monitor_lock import MonitorLease

    other_dir = tmp_path / "other"
    other_dir.mkdir()
    with MonitorLease(data_dir=tmp_path, endpoint="/tmp/one.sock"):
        with MonitorLease(data_dir=tmp_path, endpoint="/tmp/two.sock"):
            pass
        with MonitorLease(data_dir=other_dir, endpoint="/tmp/one.sock"):
            pass


def test_monitor_lease_releases_after_exception(tmp_path):
    import pytest

    from sessiongauge.monitor_lock import MonitorLease

    with pytest.raises(ValueError), MonitorLease(data_dir=tmp_path, endpoint="/tmp/exception.sock"):
        raise ValueError("test exception")

    with MonitorLease(data_dir=tmp_path, endpoint="/tmp/exception.sock"):
        pass


def test_monitor_lease_rejects_symlink_lock(tmp_path):
    import hashlib

    import pytest

    from sessiongauge.monitor_lock import MonitorLease

    run_dir = tmp_path / ".sessiongauge-run"
    run_dir.mkdir(mode=0o700)
    endpoint = "/tmp/symlink.sock"
    lock_name = (
        hashlib.sha256(
            __import__("os").fsencode(__import__("pathlib").Path(endpoint).resolve())
        ).hexdigest()
        + ".lock"
    )
    target = tmp_path / "unrelated"
    target.write_text("do not open")
    (run_dir / lock_name).symlink_to(target)

    with pytest.raises(OSError), MonitorLease(data_dir=tmp_path, endpoint=endpoint):
        pass


def test_monitor_lease_rejects_hardlink_lock(tmp_path):
    import hashlib

    import pytest

    from sessiongauge.monitor_lock import MonitorLease

    run_dir = tmp_path / ".sessiongauge-run"
    run_dir.mkdir(mode=0o700)
    endpoint = "/tmp/hardlink.sock"
    lock_name = (
        hashlib.sha256(
            __import__("os").fsencode(__import__("pathlib").Path(endpoint).resolve())
        ).hexdigest()
        + ".lock"
    )
    target = tmp_path / "unrelated"
    target.write_text("do not lock")
    (run_dir / lock_name).hardlink_to(target)

    with (
        pytest.raises(RuntimeError, match="single-link regular file"),
        MonitorLease(data_dir=tmp_path, endpoint=endpoint),
    ):
        pass


def test_monitor_lease_fd_is_not_inheritable(tmp_path):
    import os

    from sessiongauge.monitor_lock import MonitorLease

    with MonitorLease(data_dir=tmp_path, endpoint="/tmp/noninheritable.sock") as lease:
        assert not os.get_inheritable(lease._fd)


def test_monitor_lease_alias_endpoint_is_same_scope(tmp_path):
    import pytest

    from sessiongauge.monitor_lock import MonitorAlreadyRunning, MonitorLease

    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    first = MonitorLease(tmp_path, str(real / "fixture.sock"))
    second = MonitorLease(tmp_path, str(alias / "fixture.sock"))
    with first, pytest.raises(MonitorAlreadyRunning), second:
        pass
