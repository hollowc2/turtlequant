from threading import Event

from turtlequant.notifications import NotificationQueue


def test_notification_queue_runs_work_and_drains_before_close():
    done = Event()
    queue = NotificationQueue(maxsize=1)

    assert queue.submit(done.set)
    assert done.wait(1)
    queue.close()
