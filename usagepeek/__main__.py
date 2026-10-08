from .instance import claim_or_exit
from .ui import run


def main() -> None:
    claim_or_exit()
    run()


if __name__ == "__main__":
    main()
