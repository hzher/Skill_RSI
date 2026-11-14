"""Deterministic public-data fixture for resource diagnostics, never a skill."""
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from adapters import meal_plan_validation as plan


def public_plan(recipes):
    names = Counter(plan.normalized(r["name"]) for r in recipes)
    selected = [r for r in recipes if names[plan.normalized(r["name"])] == 1 and r["ingredients"]][:21]
    if len(selected) != 21: raise ValueError("Insufficient public diagnostic recipes")
    payload, events, ingredients = {"meal_plan": [], "shopping_list": []}, [], {}
    for i, recipe in enumerate(selected):
        meal = list(plan.MEALS)[i % 3]
        clock, prep = plan.MEALS[meal]
        day = plan.FIRST_DAY + timedelta(days=i // 3)
        start = datetime.combine(day, clock, ZoneInfo(plan.ZONE))
        payload["meal_plan"].append({"day": day.isoformat(), "meal_type": meal, "recipe_name": recipe["name"], "recipe_id": recipe["id"],
            "prep_time": prep if recipe.get("prep_time_minutes") is None else recipe["prep_time_minutes"]})
        events.append({"summary": "Cook: " + recipe["name"], "start": {"dateTime": start.isoformat(), "timeZone": plan.ZONE},
            "end": {"dateTime": (start + timedelta(hours=1)).isoformat(), "timeZone": plan.ZONE}})
        for ingredient in recipe["ingredients"]: ingredients.setdefault(plan.normalized(ingredient["name"]), ingredient["name"])
    payload["shopping_list"] = [{"ingredient": ingredients[key], "quantity": "as needed"} for key in sorted(ingredients)]
    return payload, events
