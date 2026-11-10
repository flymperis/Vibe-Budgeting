def post_fork(server, worker):
    import app as app_module

    app_module.arm_background_jobs()
