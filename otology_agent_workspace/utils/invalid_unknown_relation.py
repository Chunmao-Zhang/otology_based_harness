from typing import List


class Company:  # entity_type: Organization
    _id: str
    name: str
    headquartered_in: List["Country"]
