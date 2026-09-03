"""Single-instance background jobs with persisted status."""

from concurrent.futures import ThreadPoolExecutor
from papercoding.domain.models import uid, encode, now


class Jobs:
    def __init__(self, store):
        self.store = store
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="papercoding")
        with store.db() as c:
            c.execute(
                "UPDATE py_jobs SET status='interrupted',phase='服务已重启：章节修改可在运行记录中继续，其他任务请重试' WHERE status IN ('queued','running')"
            )
            c.execute("UPDATE py_runs SET status='interrupted' WHERE status='drafting'")

    def submit(self, kind, operation, context=None):
        ident = uid()
        initial = {"_context": context} if context else {}
        with self.store.db() as c:
            c.execute(
                "INSERT INTO py_jobs VALUES(?,?,'queued',0,'等待执行',?,'',?,?)",
                (ident, kind, encode(initial), now(), now()),
            )

        def worker():
            partial = dict(initial)

            def progress(percent, phase, result=None):
                nonlocal partial
                if result is not None:
                    partial = {**initial, **result}
                self.store.job_update(ident, progress=percent, phase=phase, result=partial)

            try:
                progress(0, "正在执行")
                result = operation(progress)
                self.store.job_update(
                    ident, "success", 100, "已完成", {**initial, **(result or {})}
                )
            except Exception as error:
                self.store.job_update(ident, "error", 0, "任务失败", partial, str(error))

        self.pool.submit(worker)
        return ident
