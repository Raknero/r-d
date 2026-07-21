import json
import time
from datetime import datetime
import re
from bs4 import BeautifulSoup
import undetected_chromedriver as uc
import random

# Suppress OSError [WinError 6] during undetected_chromedriver shutdown
uc.Chrome.__del__ = lambda self: None

def parse_tefas_html(html, fund_code):
    """Parses TEFAS fund analysis HTML and extracts core metrics and asset distribution."""
    soup = BeautifulSoup(html, "html.parser")
    
    def extract_and_clean(keyword):
        """Locates the keyword within the DOM and extracts the associated numerical value."""
        element = soup.find(string=re.compile(keyword, re.IGNORECASE))
        if element:
            parent_tag = element.parent
            container = parent_tag.find_parent('div')
            if container:
                for tag in container.find_all(['span', 'div', 'p', 'h3', 'h4', 'strong']):
                    text = tag.text.strip()
                    if any(c.isdigit() for c in text) and text != parent_tag.text.strip():
                        return text
            
            sibling = parent_tag.find_next_sibling(['span', 'div', 'p', 'strong'])
            if sibling and sibling.text.strip():
                return sibling.text.strip()
        return "0"

    def convert_to_float(text):
        """Removes non-numeric characters and converts the string to float."""
        if not text or text == "0": return 0.0
        cleaned = re.sub(r'[^\d\.,]', '', text)
        if not cleaned: return 0.0
        return float(cleaned.replace('.', '').replace(',', '.'))

    # Extract core financial metrics
    price_str = extract_and_clean("Son Fiyat")
    total_val_str = extract_and_clean("Fon Toplam Değer")
    shares_str = extract_and_clean("Pay")
    investors_str = extract_and_clean("Yatırımcı Sayısı")

    # JSON keys are kept in Turkish to maintain compatibility with the data pipeline
    parsed_data = {
        "Tarih": datetime.now().strftime("%d.%m.%Y"),
        "Fiyat": convert_to_float(price_str),
        "Pay": int(convert_to_float(shares_str)),
        "ToplamDeger": convert_to_float(total_val_str),
        "Yatirimci": int(convert_to_float(investors_str)),
        "PazarPayi": 0.0,
        "Varliklar": {}
    }
    
    # Parse asset distribution table
    asset_header = soup.find(string=re.compile("Varlık Türü", re.IGNORECASE))
    if asset_header:
        table = asset_header.find_parent("table")
        if table:
            rows = table.find_all("tr")
            for row in rows:
                cols = row.find_all(["td", "th"])
                if len(cols) >= 2:
                    name = cols[0].text.strip()
                    if name and name != "Varlık Türü" and "Oran" not in name:
                        ratio_str = re.sub(r'[^\d\.,]', '', cols[1].text)
                        if ratio_str:
                            try:
                                parsed_data["Varliklar"][name] = float(ratio_str.replace(',', '.'))
                            except ValueError:
                                pass
    
    return parsed_data

if __name__ == "__main__":
    print("[SYSTEM] Initializing TEFAS Data Scraper...")
    
    target_funds = ["TLY", "PHE", "YAS"] 
    
    options = uc.ChromeOptions()
    options.add_argument("--window-size=1920,1080")
    # options.add_argument("--headless=new") # Uncomment for headless execution
    
    try:
        driver = uc.Chrome(options=options, version_main=150)
        
        # Fetch initial cookies to bypass F5 BIG-IP WAF
        print("[NETWORK] Fetching initial security cookies...")
        driver.get("https://www.tefas.gov.tr")
        time.sleep(8) 
        
        for code in target_funds:
            print(f"[{code}] Fetching fund data...")
            url = f"https://www.tefas.gov.tr/tr/fon-detayli-analiz/{code}"
            
            driver.get(url)
            time.sleep(15) # Wait for dynamic Tailwind DOM to load
            
            html = driver.page_source
            
            if "The requested URL was rejected" in html or "Support ID" in html:
                print(f"[ERROR] WAF blocked the request for {code}. IP might be soft-banned.")
                continue
            
            data = parse_tefas_html(html, code)
            
            if data and data["Fiyat"] > 0:
                filename = f"gunluk_{code.lower()}.json"
                with open(filename, "w", encoding="utf-8") as file:
                    json.dump(data, file, ensure_ascii=False, indent=4)
                print(f"[SUCCESS] Data successfully exported to {filename}")
            else:
                print(f"[WARNING] Failed to parse valid price data for {code}.")
            
            # Random delay between requests to simulate human behavior and avoid rate limits
            if code != target_funds[-1]:
                delay = random.randint(4, 8)
                time.sleep(delay)
                
    except Exception as e:
        print(f"[CRITICAL] Unhandled exception occurred: {e}")
        
    finally:
        print("[SYSTEM] Closing browser session and cleaning up.")
        try:
            driver.quit()
        except:
            pass