from typing import List


class Company:
    _id: str
    name: str
    headquartered_in: List["Country"]
