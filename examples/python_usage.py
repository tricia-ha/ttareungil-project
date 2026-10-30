"""Run from any directory after installing the project dependencies."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from project_service import ProjectService


def main():
    origin = {"name": "서울역", "latitude": 37.5568, "longitude": 126.9723}
    destination = {
        "name": "여의도 한강공원",
        "latitude": 37.5284,
        "longitude": 126.9326,
    }
    with ProjectService() as service:
        result = service.find_routes(
            origin, destination, departure="auto", mode="transit"
        )
        print(result["status"], result["termination_reason"])
        for i, route in enumerate(result["routes"], 1):
            print(
                i,
                round(route["total_minutes"], 2),
                [leg["mode"] for leg in route["legs"]],
            )
        # Optional screenshot flow; confirm only after comparing the extraction with the image.
        # profile_id=service.create_profile()['profile_id']
        # draft=service.preview_screenshot(profile_id,'C:/photos/ride.png')
        # print(draft)
        # service.confirm_screenshot(profile_id,draft['draft_id'],0,confirmed=True)
        # result=service.find_routes(origin,destination,mode='integrated',profile_id=profile_id)


if __name__ == "__main__":
    main()
