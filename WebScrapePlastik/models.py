from dataclasses import dataclass


@dataclass
class Place:
    name: str
    address: str
    phone: str
    lat: float
    lng: float
