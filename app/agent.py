# ruff: noqa
# Copyright 2026 Google LLC

import base64
import datetime
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any, Optional
from zoneinfo import ZoneInfo

import google.auth
import google.auth.transport.requests

from a2ui.basic_catalog.provider import BasicCatalog
from a2ui.schema.manager import A2uiSchemaManager

from google import genai
from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App
from google.adk.code_executors import AgentEngineSandboxCodeExecutor
from google.adk.memory import VertexAiMemoryBankService
from google.adk.models import Gemini
from google.adk.tools import ToolContext
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from google.cloud import firestore, storage
from google.genai import types

from .a2ui_utils import a2ui_callback

# Hardcode project ID, bucket name, and memory engine ID as strings for Agent Platform compatibility
PROJECT_ID = "qwiklabs-gcp-02-9a8a47b644e5"
BUCKET_NAME = "travel-concierge-media-qwiklabs-gcp-02-9a8a47b644e5"
MEMORY_ENGINE_ID = "2808198876828270592"

db = firestore.Client(project=PROJECT_ID)
storage_client = storage.Client(project=PROJECT_ID)
genai_client = genai.Client(vertexai=True, project=PROJECT_ID, location="global")

# Memory Bank callback to write durable memories across sessions
async def generate_memories_callback(callback_context: CallbackContext):
    await callback_context.add_session_to_memory()
    return None

def memory_bank_service_builder():
    return VertexAiMemoryBankService(
        project=PROJECT_ID,
        location="us-east1",
        agent_engine_id=MEMORY_ENGINE_ID,
    )

# Initialize Agent Engine Sandbox Code Executor from deployment_metadata.json if available
DEPLOYMENT_METADATA_FILE = os.path.join(os.path.dirname(__file__), "..", "deployment_metadata.json")
agent_engine_id = None
if os.path.exists(DEPLOYMENT_METADATA_FILE):
    try:
        with open(DEPLOYMENT_METADATA_FILE, "r") as f:
            meta = json.load(f)
            agent_engine_id = meta.get("remote_agent_runtime_id")
    except Exception:
        pass

code_executor = AgentEngineSandboxCodeExecutor(agent_engine_resource_name=agent_engine_id)


def search_destinations(category: Optional[str] = None, country: Optional[str] = None) -> list[dict[str, Any]]:
    """Search travel destinations from the Firestore catalog.

    Args:
        category: Optional category filter (e.g. 'Cultural & Historical', 'Nature & Adventure', 'Art & Culinary', 'Beach & Scenic').
        country: Optional country filter (e.g. 'Japan', 'France', 'Canada', 'Greece').

    Returns:
        A list of matching travel destination objects.
    """
    collection_ref = db.collection("destinations")
    query = collection_ref

    if category:
        query = query.where("category", "==", category)
    if country:
        query = query.where("country", "==", country)

    results = []
    for doc in query.stream():
        data = doc.to_dict()
        data["id"] = doc.id
        results.append(data)

    # Fallback to returning all destinations if no direct filter match
    if not results and (category or country):
        for doc in collection_ref.stream():
            data = doc.to_dict()
            data["id"] = doc.id
            results.append(data)

    return results


def add_destination(
    name: str,
    country: str,
    category: str,
    description: str,
    highlights: list[str],
    recommended_season: str,
    estimated_cost_per_day: float,
) -> str:
    """Add a new travel destination to the Firestore catalog.

    Args:
        name: Name of the destination (e.g. 'Rome').
        country: Country name (e.g. 'Italy').
        category: Category of travel (e.g. 'Historical & Culinary').
        description: A description of what makes this destination special.
        highlights: Key attractions or highlights.
        recommended_season: Best time/season to visit.
        estimated_cost_per_day: Estimated daily budget in USD.

    Returns:
        A success message with the created destination ID.
    """
    doc_id = f"dest_{name.lower().replace(' ', '_')}"
    doc_ref = db.collection("destinations").document(doc_id)
    dest_data = {
        "id": doc_id,
        "name": name,
        "country": country,
        "category": category,
        "description": description,
        "highlights": highlights,
        "recommended_season": recommended_season,
        "estimated_cost_per_day": estimated_cost_per_day,
    }
    doc_ref.set(dest_data)
    return f"Successfully added {name} ({doc_id}) to the destinations catalog."


def calculate_trip_budget(
    destination: str,
    days: int,
    travelers: int = 1,
    travel_style: str = "standard",
) -> dict[str, Any]:
    """Calculate an itemized travel budget for a trip based on destination costs and duration.

    Args:
        destination: Name of the destination (e.g. 'Kyoto', 'Paris', 'Rome', 'Banff National Park', 'Santorini').
        days: Number of days for the trip.
        travelers: Number of people traveling. Default is 1.
        travel_style: Style of travel: 'budget' (0.7x multiplier), 'standard' (1.0x), or 'luxury' (1.8x).

    Returns:
        A dictionary containing an itemized cost breakdown (lodging, food, activities, transport, total).
    """
    doc_id = f"dest_{destination.lower().replace(' ', '_')}"
    doc_ref = db.collection("destinations").document(doc_id).get()

    if doc_ref.exists:
        base_daily_cost = float(doc_ref.to_dict().get("estimated_cost_per_day", 150))
    else:
        base_daily_cost = 150.0

    style_multipliers = {"budget": 0.7, "standard": 1.0, "luxury": 1.8}
    multiplier = style_multipliers.get(travel_style.lower(), 1.0)
    daily_per_person = base_daily_cost * multiplier

    lodging = round(daily_per_person * 0.45 * days * travelers, 2)
    food = round(daily_per_person * 0.30 * days * travelers, 2)
    activities = round(daily_per_person * 0.15 * days * travelers, 2)
    transport = round(daily_per_person * 0.10 * days * travelers, 2)
    total = round(lodging + food + activities + transport, 2)

    return {
        "destination": destination,
        "days": days,
        "travelers": travelers,
        "travel_style": travel_style,
        "daily_cost_per_person": round(daily_per_person, 2),
        "lodging": lodging,
        "food": food,
        "activities": activities,
        "transport": transport,
        "total_estimated_budget": total,
    }


def get_destination_info(destination: str) -> dict[str, Any]:
    """Fetch real-time summary, historical overview, photo thumbnail, and coordinates for a destination using Wikipedia REST API.

    Args:
        destination: Name of the location or destination (e.g. 'Kyoto', 'Paris', 'Banff National Park', 'Santorini').

    Returns:
        A dictionary containing destination title, summary, description, thumbnail_url, coordinates, and article link.
    """
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(destination)}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "TravelConciergeApp/1.0 (contact@example.com)"}
    )
    try:
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode("utf-8"))
            return {
                "title": data.get("title", destination),
                "description": data.get("description", ""),
                "summary": data.get("extract", ""),
                "thumbnail_url": data.get("thumbnail", {}).get("source", ""),
                "coordinates": data.get("coordinates", {}),
                "wiki_url": data.get("content_urls", {}).get("desktop", {}).get("page", ""),
            }
    except Exception as e:
        return {"destination": destination, "error": f"Failed to fetch live info: {str(e)}"}


def geocode_address(address: str) -> dict[str, Any]:
    """Convert an address or location name into geographic coordinates (latitude and longitude) using Google Geocoding API.

    Args:
        address: The address or place name to geocode (e.g. 'Kyoto Station, Japan' or 'Eiffel Tower, Paris').

    Returns:
        A dictionary containing formatted_address, latitude, longitude, and place_id.
    """
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        return {"error": "GOOGLE_MAPS_API_KEY environment variable is not configured."}

    encoded_address = urllib.parse.quote(address)
    url = f"https://maps.googleapis.com/maps/api/geocode/json?address={encoded_address}&key={api_key}"

    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode("utf-8"))
            if data.get("status") == "OK" and data.get("results"):
                top_result = data["results"][0]
                loc = top_result.get("geometry", {}).get("location", {})
                return {
                    "formatted_address": top_result.get("formatted_address", address),
                    "latitude": loc.get("lat"),
                    "longitude": loc.get("lng"),
                    "place_id": top_result.get("place_id", ""),
                }
            return {"error": f"Geocoding failed with status: {data.get('status')}"}
    except Exception as e:
        return {"error": f"Geocoding API error: {str(e)}"}


def find_nearby_places(
    latitude: float,
    longitude: float,
    place_type: str = "tourist_attraction",
    radius: float = 2000.0,
) -> list[dict[str, Any]]:
    """Find nearby places of a given type around a location using Places API (New) searchNearby REST endpoint.

    Args:
        latitude: Center latitude coordinate.
        longitude: Center longitude coordinate.
        place_type: Type of place to search for (e.g. 'tourist_attraction', 'restaurant', 'museum', 'lodging', 'park').
        radius: Radius in meters around the center location (default 2000 meters).

    Returns:
        A list of nearby place objects containing name, address, location (lat/lng), and types.
    """
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        return [{"error": "GOOGLE_MAPS_API_KEY environment variable is not configured."}]

    url = "https://places.googleapis.com/v1/places:searchNearby"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.location,places.types",
    }
    payload = {
        "includedTypes": [place_type],
        "maxResultCount": 5,
        "locationRestriction": {
            "circle": {
                "center": {
                    "latitude": latitude,
                    "longitude": longitude,
                },
                "radius": radius,
            }
        },
    }

    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode("utf-8"))
            places = data.get("places", [])
            results = []
            for p in places:
                results.append({
                    "name": p.get("displayName", {}).get("text", ""),
                    "address": p.get("formattedAddress", ""),
                    "location": p.get("location", {}),
                    "types": p.get("types", []),
                })
            return results
    except Exception as e:
        return [{"error": f"Places API error: {str(e)}"}]


def generate_destination_image(
    prompt: str,
    destination: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Generate a travel image or postcard for a destination using gemini-3.1-flash-lite-image in the global region.

    Saves the image as an artifact in Playground's Artifacts panel and uploads it to the public Cloud Storage bucket.

    Args:
        prompt: A detailed prompt describing the travel image/postcard to generate (e.g., 'A vibrant postcard of Kyoto Japan with cherry blossoms and temples').
        destination: Name of the destination (e.g. 'Kyoto', 'Paris', 'Santorini').
        tool_context: ADK ToolContext automatically injected by runtime for saving artifacts.

    Returns:
        A dictionary containing destination, artifact_filename, image_url, and a success message.
    """
    try:
        response = genai_client.models.generate_content(
            model="gemini-3.1-flash-lite-image",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
            ),
        )

        image_bytes = None
        mime_type = "image/jpeg"

        if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if part.inline_data:
                    image_bytes = part.inline_data.data
                    if part.inline_data.mime_type:
                        mime_type = part.inline_data.mime_type
                    break

        if not image_bytes:
            return {"error": "Failed to extract generated image bytes from model response."}

        timestamp = int(time.time())
        dest_slug = destination.lower().replace(" ", "_")
        filename = f"postcard_{dest_slug}_{timestamp}.jpg"

        # 1. Save artifact for Playground Artifacts panel
        artifact_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
        tool_context.save_artifact(filename=filename, artifact=artifact_part)

        # 2. Upload to public Cloud Storage bucket
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(f"postcards/{filename}")
        blob.upload_from_string(image_bytes, content_type=mime_type)

        public_url = f"https://storage.googleapis.com/{BUCKET_NAME}/postcards/{filename}"

        return {
            "destination": destination,
            "artifact_filename": filename,
            "image_url": public_url,
            "message": f"Successfully generated and uploaded postcard image for {destination}.",
        }
    except Exception as e:
        return {"error": f"Image generation failed: {str(e)}"}


def get_weather(query: str) -> str:
    """Simulates a web search for weather information.

    Args:
        query: A string containing the location to get weather information for.

    Returns:
        Simulated weather information.
    """
    if "sf" in query.lower() or "san francisco" in query.lower():
        return "It's 60 degrees and foggy."
    elif "kyoto" in query.lower() or "japan" in query.lower():
        return "It's 68 degrees and pleasant."
    elif "paris" in query.lower() or "france" in query.lower():
        return "It's 65 degrees and sunny."
    elif "banff" in query.lower() or "canada" in query.lower():
        return "It's 52 degrees and crisp."
    elif "santorini" in query.lower() or "greece" in query.lower():
        return "It's 75 degrees and clear."
    return "It's 70 degrees and clear."


def get_current_time(query: str) -> str:
    """Simulates getting the current time for a city.

    Args:
        query: The name of the city to get the current time for.

    Returns:
        Current local time.
    """
    if "sf" in query.lower() or "san francisco" in query.lower():
        tz_identifier = "America/Los_Angeles"
    elif "kyoto" in query.lower() or "japan" in query.lower():
        tz_identifier = "Asia/Tokyo"
    elif "paris" in query.lower() or "france" in query.lower():
        tz_identifier = "Europe/Paris"
    else:
        tz_identifier = "UTC"

    tz = ZoneInfo(tz_identifier)
    now = datetime.datetime.now(tz)
    return f"The current time for {query} is {now.strftime('%Y-%m-%d %H:%M:%S %Z%z')}"


def generate_destination_video(
    prompt: str,
    destination: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Generate a short video for a travel destination using Google's Omni model (gemini-omni-flash-preview) in the global region.

    Saves the video with tool_context.save_artifact so it shows up in Playground's Artifacts panel,
    and uploads the video bytes to the public Cloud Storage bucket, returning its public HTTPS URL.

    Args:
        prompt: A detailed prompt describing the travel video to generate (e.g. 'A short video clip of waves crashing on a beach in Santorini').
        destination: Name of the destination (e.g. 'Santorini', 'Kyoto', 'Paris', 'Rome').
        tool_context: ADK ToolContext automatically injected by runtime for saving artifacts.

    Returns:
        A dictionary containing destination, artifact_filename, video_url, and a success message.
    """
    try:
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google.auth.transport.requests.Request())

        url = f"https://aiplatform.googleapis.com/v1beta1/projects/{PROJECT_ID}/locations/global/interactions"
        headers = {
            "Authorization": f"Bearer {creds.token}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "gemini-omni-flash-preview",
            "input": [
                {"type": "text", "text": prompt}
            ],
            "response_format": [
                {
                    "type": "video",
                    "aspect_ratio": "16:9",
                    "resolution": "720p",
                    "duration": "5s"
                }
            ],
            "generation_config": {
                "video_config": {
                    "task": "text_to_video"
                }
            }
        }

        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req) as resp:
            res = json.loads(resp.read().decode("utf-8"))

        video_bytes = None
        mime_type = "video/mp4"

        for step in res.get("steps", []):
            if step.get("type") == "model_output":
                for content in step.get("content", []):
                    if "bytesBase64Encoded" in content:
                        video_bytes = base64.b64decode(content["bytesBase64Encoded"])
                        mime_type = content.get("mime_type", mime_type)
                        break
                    elif "data" in content:
                        video_bytes = base64.b64decode(content["data"])
                        mime_type = content.get("mime_type", mime_type)
                        break
                    elif "uri" in content:
                        gcs_uri = content["uri"]
                        if gcs_uri.startswith("gs://"):
                            parts = gcs_uri[5:].split("/", 1)
                            b_name, b_path = parts[0], parts[1]
                            video_bytes = storage_client.bucket(b_name).blob(b_path).download_as_bytes()
                            mime_type = content.get("mime_type", mime_type)
                        break

        if not video_bytes:
            return {"error": "Failed to extract generated video bytes from model response."}

        timestamp = int(time.time())
        dest_slug = destination.lower().replace(" ", "_")
        filename = f"video_{dest_slug}_{timestamp}.mp4"

        # 1. Save artifact for Playground Artifacts panel
        artifact_part = types.Part.from_bytes(data=video_bytes, mime_type=mime_type)
        tool_context.save_artifact(filename=filename, artifact=artifact_part)

        # 2. Upload to public Cloud Storage bucket
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(f"videos/{filename}")
        blob.upload_from_string(video_bytes, content_type=mime_type)

        public_url = f"https://storage.googleapis.com/{BUCKET_NAME}/videos/{filename}"

        return {
            "destination": destination,
            "artifact_filename": filename,
            "video_url": public_url,
            "message": f"Successfully generated and uploaded video for {destination}.",
        }
    except Exception as e:
        return {"error": f"Video generation failed: {str(e)}"}


schema_manager = A2uiSchemaManager(
    version="0.8",
    catalogs=[BasicCatalog.get_config("0.8")],
)

a2ui_instruction = schema_manager.generate_system_prompt(
    role_description=(
        "You are Travel Concierge AI, a helpful and knowledgeable travel planning assistant. "
        "You help users explore travel destinations, plan itineraries, calculate trip budgets, run Python calculations, "
        "geocode addresses, find nearby points of interest, generate destination postcards/images, generate destination videos, fetch real-time location details, "
        "look up weather/time info, and remember user preferences, facts, and allergies across sessions. "
        "MEMORY & ALLERGIES: Pay special attention to any allergies mentioned by the user (such as food allergies, gluten/dairy intolerances, "
        "peanut/shellfish allergies, insect/latex/medical allergies). Store and remember all user allergies across sessions via memory. "
        "Always check preloaded user memories for any stated allergies before recommending dining options, activities, or travel itineraries, "
        "and explicitly confirm or tailor your suggestions to keep the user safe."
    ),
    workflow_description="Analyze the request and return structured UI when appropriate (e.g. for destination cards, nearby place listings, trip budget breakdowns, or generated travel postcards/videos).",
    ui_description=(
        "Keep every surface tiny and flat: ONE Card > ONE Column > a few Text rows. "
        "Never nest a Card inside a Card. "
        "Use ONLY these components: Card, Column, Row, Text, and Image. Do not use "
        "Table or Heading (unsupported), or Buttons, actions, or forms (they do "
        "nothing in adk web). "
        "You may include one Image component, but only when you have a public https "
        "URL for the image (for example the URL an image tool returns after uploading "
        "to a public bucket). Set the Image url to that exact https link, for example "
        "{\"Image\": {\"url\": {\"literalString\": \"https://...\"}}}. Never point an "
        "Image at a bare filename, an artifact name, or a non-http(s) path. If you do "
        "not have a public URL, add a short Text line noting the image instead. "
        "No markdown in text; use the usageHint property ('h1', 'h2', 'body') for "
        "headings and emphasis. "
        "Output ONLY the raw A2UI JSON array — no prose, and never wrap it in "
        "<a2a_datapart_json> tags or 'kind'/'data'/'metadata' objects."
    ),
    include_schema=True,
    include_examples=True,
)

root_agent = Agent(
    name="root_agent",
    model=Gemini(
        model="gemini-2.5-flash",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    code_executor=code_executor,
    instruction=a2ui_instruction,
    tools=[PreloadMemoryTool(), search_destinations, add_destination, calculate_trip_budget, get_destination_info, geocode_address, find_nearby_places, generate_destination_image, generate_destination_video, get_weather, get_current_time],
    after_agent_callback=generate_memories_callback,
    after_model_callback=a2ui_callback,
)

app = App(
    root_agent=root_agent,
    name="app",
)
