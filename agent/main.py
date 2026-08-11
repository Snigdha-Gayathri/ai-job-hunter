import os
from datetime import datetime, timezone


def main():
    print("AI Job Hunter started.")
    print(f"UTC time: {datetime.now(timezone.utc).isoformat()}")

    print("Job search agent is alive.")


if __name__ == "__main__":
    main()
