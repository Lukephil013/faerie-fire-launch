from __future__ import annotations

import threading

from livingpc.maintenance import MaintenanceBusy, maintenance_lock


def test_maintenance_lock_is_reentrant_in_same_thread(tmp_path):
    lock = tmp_path / "maintenance.lock"
    with maintenance_lock(path=str(lock), timeout=0):
        with maintenance_lock(path=str(lock), timeout=0):
            assert lock.exists()


def test_maintenance_lock_serializes_threads(tmp_path):
    lock = tmp_path / "maintenance.lock"
    entered = threading.Event()
    release = threading.Event()

    def owner():
        with maintenance_lock(path=str(lock), timeout=1):
            entered.set()
            release.wait(2)

    thread = threading.Thread(target=owner)
    thread.start()
    assert entered.wait(1)
    try:
        try:
            with maintenance_lock(path=str(lock), timeout=0):
                raise AssertionError("second thread entered the maintenance lock")
        except MaintenanceBusy:
            pass
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()

