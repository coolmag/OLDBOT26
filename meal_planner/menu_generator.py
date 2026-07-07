# menu_generator.py
import random

def generate_menu(recipes, days):
    """
    Генерирует случайное меню на заданное количество дней.
    Пока что выбирает по одному блюду на день (упрощенная логика).
    """
    if not recipes:
        return None, "База данных рецептов пуста."

    if len(recipes) < days:
        return None, f"Недостаточно уникальных рецептов в базе. В базе {len(recipes)}, а вы запросили на {days} дней."

    # Выбираем `days` случайных, неповторяющихся рецептов
    daily_menu_items = random.sample(recipes, days)
    
    # Формируем план
    menu_plan = {}
    for i, recipe in enumerate(daily_menu_items):
        day_number = i + 1
        menu_plan[f"День {day_number}"] = recipe

    return menu_plan, None
