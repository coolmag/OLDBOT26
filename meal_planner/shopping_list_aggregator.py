# shopping_list_aggregator.py
from collections import defaultdict

def aggregate_shopping_list(menu):
    """
    Агрегирует ингредиенты из сгенерированного меню в единый список покупок.
    """
    if not menu:
        return None, "Меню для агрегации не предоставлено."

    # Используем defaultdict для удобного суммирования
    # Структура: { 'продукт': { 'единица_измерения': количество, ... } }
    shopping_list = defaultdict(lambda: defaultdict(int))

    # Проходим по каждому дню и каждому рецепту в меню
    for day, recipe in menu.items():
        for ingredient in recipe['ingredients']:
            product_name = ingredient['product_name']
            quantity = ingredient['quantity']
            unit = ingredient['unit']
            
            # Суммируем количество для данной единицы измерения
            shopping_list[product_name][unit] += quantity
            
    # Преобразуем в более удобный для вывода формат
    # { 'продукт': ['X г', 'Y шт'] }
    formatted_list = defaultdict(list)
    for product, units in sorted(shopping_list.items()):
        for unit, total_quantity in units.items():
            # Обрабатываем дроби для "шт"
            if unit == 'шт' and isinstance(total_quantity, float) and not total_quantity.is_integer():
                 # Округляем до большего целого, т.к. нельзя купить 0.5 лимона
                 total_quantity = int(total_quantity) + (1 if total_quantity % 1 > 0 else 0)
            
            formatted_list[product].append(f"{total_quantity} {unit}")

    return formatted_list, None
