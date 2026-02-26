import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
from datetime import datetime

url_dict = {
    "NPR":" https://www.npr.org/tags/127994355/china",
    "AP": "https://apnews.com/hub/china",
    "RoW": "https://restofworld.org/region/china/",
    "RoW_Out": "https://restofworld.org/series/china-outside-china/",
    "CMP": "https://chinamediaproject.org/"
}



def scrape(website, website_url):
    print(website_url)

    if website == "NPR":
        feature = "h2"
    elif website == "AP":
        feature = "h3"
    elif website == "RoW_Out":
        feature = "a.grid-story__link.article-link"
    elif website == "RoW":
        feature = "a.article-link"
    elif website == "CMP":
        feature = "a.uk-display-block.uk-panel.uk-margin-remove-first-child.uk-link-toggle"
    else:
        return []

    items = []
    response = requests.get(website_url)
    soup = BeautifulSoup(response.content, 'html.parser')

    titles = soup.select(feature)

    for title in titles:
        # print(title)
        # print("")
        link = None
        title_text = None
        # --- RoW structure ---
        if "RoW" in website:
            link = title.get('href')
            h2 = title.find('h2')
            if h2:
                title_text = h2.get_text(strip=True)
        # --- CMP structure ---
        elif "CMP" in website:
            link = title.get('href')
            h3 = title.find('h3')
            if h3:
                title_text = h3.get_text(strip=True)
        # --- NPR / AP structure ---
        else:
            link_tag = title.find('a')
            if link_tag:
                link = link_tag.get('href')
                title_text = link_tag.get_text(strip=True)

        # Only append if both exist
        if link and title_text:
            items.append({
                'title': title_text,
                'link': link
            })
            print(f"Link: {link}")
            print(f"Title: {title_text}")

    return items


# Function to create RSS feed
def create_rss_feed(site_name, items, filename):
    rss = ET.Element('rss', version='2.0')
    channel = ET.SubElement(rss, 'channel')
    ET.SubElement(channel, 'title').text = f'{site_name} China News'
    ET.SubElement(channel, 'link').text = f'https://github.com/jessicabatke/more_news_rss'
    ET.SubElement(channel, 'description').text = f'China news from {site_name}'
    ET.SubElement(channel, 'lastBuildDate').text = datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT')
    
    for article in items:
        item = ET.SubElement(channel, 'item')
        ET.SubElement(item, 'title').text = article['title']
        ET.SubElement(item, 'link').text = article['link']
        ET.SubElement(item, 'description').text = article['title']
        ET.SubElement(item, 'pubDate').text = datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT')
        ET.SubElement(item, 'guid').text = article['link']
    
    tree = ET.ElementTree(rss)
    ET.indent(tree, space='  ')
    tree.write(filename, encoding='unicode', xml_declaration=True)
    
    print(f"Generated {filename} with {len(items)} articles")


for key,value in url_dict.items():
    print("")
    print("")
    print("")
    print("")
    items = scrape(key, url_dict[key])
    filename = f'{key.lower()}_china_feed.xml'
    create_rss_feed(key, items, filename)


