"""Human-like behavior simulation: random delays, mouse wheel scrolling."""

import asyncio
import random

from playwright.async_api import Page

from config import ACTION_DELAY, PAGE_DELAY, SCROLL_DELAY


async def random_delay(range_tuple: tuple[float, float] = ACTION_DELAY) -> None:
    delay = random.uniform(*range_tuple)
    await asyncio.sleep(delay)


async def page_delay() -> None:
    await random_delay(PAGE_DELAY)


async def scroll_delay() -> None:
    await random_delay(SCROLL_DELAY)


async def human_scroll(page: Page, distance: int = 800) -> None:
    """Scroll down using mouse wheel with slight randomization."""
    # Randomize scroll distance slightly
    actual_distance = distance + random.randint(-100, 100)

    # Move mouse to a random position in the viewport first
    x = random.randint(400, 800)
    y = random.randint(300, 500)
    await page.mouse.move(x, y)

    # Scroll in smaller increments to look more human
    scrolled = 0
    while scrolled < actual_distance:
        chunk = min(random.randint(80, 200), actual_distance - scrolled)
        await page.mouse.wheel(0, chunk)
        scrolled += chunk
        await asyncio.sleep(random.uniform(0.05, 0.15))

    await scroll_delay()
