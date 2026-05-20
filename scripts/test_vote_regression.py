"""Regression test: verify all BOS items 85-95 get votes from meeting 4161."""
import asyncio, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
from playwright.async_api import async_playwright
from scraper.votes import extract_votes_from_summary

async def test_vote_parsing():
    """Test that items 85-95 from meeting 4161 all receive votes."""
    vote_items = [
        {"agenda_item_number": n, "c_number": ""}
        for n in range(85, 96)
    ]
    
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        url = 'https://mccobagenda.databankcloud.com/AgendaOnline/Meetings/ViewMeeting?id=4161&doctype=3'
        
        supervisors, votes = await extract_votes_from_summary(page, url, vote_items)
    
    # Check which items have votes
    vote_nums = {v['agenda_item_number'] for v in votes}
    all_nums = set(range(85, 96))
    missing = all_nums - vote_nums
    
    passed = len(missing) == 0
    
    print(f'Test: BOS meeting 4161 items 85-95 vote extraction')
    print(f'  Expected: 11 items with votes')
    print(f'  Got: {len(votes)} items with votes')
    print(f'  Missing items: {sorted(missing) if missing else "NONE"}')
    
    for v in sorted(votes, key=lambda x: x['agenda_item_number']):
        n_yes = sum(1 for sv in v['supervisor_votes'] if sv['vote'] == 'yes')
        n_no = sum(1 for sv in v['supervisor_votes'] if sv['vote'] == 'no')
        print(f'  Item {v["agenda_item_number"]}: {v["motion_result"]}, yes={n_yes}, no={n_no}')
    
    if passed:
        print('\nPASS: All items received votes')
    else:
        print(f'\nFAIL: {len(missing)} items missing votes: {sorted(missing)}')
    
    return passed

if __name__ == '__main__':
    result = asyncio.run(test_vote_parsing())
    sys.exit(0 if result else 1)
