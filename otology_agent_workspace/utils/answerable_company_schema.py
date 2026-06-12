from typing import List, Optional


class Company:  # entity_type: Organization
    _id: str
    name: str
    country: Optional[str]
    operates_in_industry: List["Industry"]


class Industry:  # entity_type: BusinessDomain
    _id: str
    name: str
    operates_in_industry_r: List["Company"]  # reverse
