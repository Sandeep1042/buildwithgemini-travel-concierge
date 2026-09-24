import os
from google.cloud import firestore

# Hardcoded project ID as required to prevent Agent Engine project number lookup issues
PROJECT_ID = "qwiklabs-gcp-02-9a8a47b644e5"

db = firestore.Client(project=PROJECT_ID)

DESTINATIONS = [
    {
        "id": "dest_kyoto",
        "name": "Kyoto",
        "country": "Japan",
        "category": "Cultural & Historical",
        "description": "Japan's ancient capital renowned for classical Buddhist temples, gardens, imperial palaces, Shinto shrines, and traditional wooden houses.",
        "highlights": ["Fushimi Inari Shrine", "Arashiyama Bamboo Grove", "Kinkaku-ji (Golden Pavilion)"],
        "recommended_season": "Spring / Autumn",
        "estimated_cost_per_day": 150
    },
    {
        "id": "dest_paris",
        "name": "Paris",
        "country": "France",
        "category": "Art & Culinary",
        "description": "A global center for art, fashion, gastronomy, and culture, known for its café culture and landmark architecture.",
        "highlights": ["Eiffel Tower", "Louvre Museum", "Notre-Dame Cathedral"],
        "recommended_season": "Spring / Early Summer",
        "estimated_cost_per_day": 200
    },
    {
        "id": "dest_banff",
        "name": "Banff National Park",
        "country": "Canada",
        "category": "Nature & Adventure",
        "description": "Canada's oldest national park in the heart of the Rocky Mountains, featuring turquoise glacial lakes and majestic mountain peaks.",
        "highlights": ["Lake Louise", "Moraine Lake", "Banff Upper Hot Springs"],
        "recommended_season": "Summer / Winter",
        "estimated_cost_per_day": 180
    },
    {
        "id": "dest_santorini",
        "name": "Santorini",
        "country": "Greece",
        "category": "Beach & Scenic",
        "description": "Iconic Aegean island famous for whitewashed cliffside villages, blue-domed churches, and stunning sunsets over the caldera.",
        "highlights": ["Oia Sunset Viewpoint", "Red Beach", "Ancient Akrotiri"],
        "recommended_season": "May to October",
        "estimated_cost_per_day": 220
    }
]

def seed_database():
    print(f"Seeding Firestore collection 'destinations' for project {PROJECT_ID}...")
    collection_ref = db.collection("destinations")
    for dest in DESTINATIONS:
        doc_ref = collection_ref.document(dest["id"])
        doc_ref.set(dest)
        print(f"  ✓ Added destination: {dest['name']} ({dest['id']})")
    print("Seeding complete!")

if __name__ == "__main__":
    seed_database()
