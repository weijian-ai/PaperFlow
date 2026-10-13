import paperflow


# 打印当前 paperflow 包的版本号
def cmd_version() -> None:
    print(paperflow.__version__)
