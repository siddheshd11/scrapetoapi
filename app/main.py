from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
import requests
from bs4 import BeautifulSoup
import uuid
from typing import Dict, Any, List, Optional
from urllib.parse import urljoin, urlparse
import time

import logging
import os



app = FastAPI(title="ScrapeToAPI")

# Setup templates and static files
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# In-memory storage for now
scraped_data = {}
scrape_cache = {}
CACHE_DURATION = 7200

# Add at the top of your file
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@app.on_event("startup")
async def startup_event():
    logger.info("🚀 ScrapeToAPI is starting up!")
    logger.info(f"Port: {os.getenv('PORT', '8000')}")
    logger.info(f"Templates directory exists: {os.path.exists('app/templates')}")
    logger.info(f"Static directory exists: {os.path.exists('app/static')}")

@app.get("/debug")
async def debug_info():
    return {
        "port": os.getenv('PORT', 'Not set'),
        "templates_exist": os.path.exists('app/templates'),
        "static_exist": os.path.exists('app/static'),
        "working_directory": os.getcwd(),
        "files": os.listdir('.')
    }


def get_cache_key(url: str) -> str:
    """Generate cache key from URL"""
    return f"cache_{hash(url)}"

def is_cache_valid(cache_entry) -> bool:
    """Check if cache entry is still valid"""
    return time.time() - cache_entry['timestamp'] < CACHE_DURATION

def element_to_dict(element, base_url: str, parent_path: str = "", position: int = 1) -> Dict[str, Any]:
    """Convert a BeautifulSoup element to a dictionary with proper XPath structure"""
    if element.name is None:  # Text node
        text = str(element).strip()
        if text and len(text) > 3:  # Only meaningful text
            return {
                "type": "text",
                "content": text,
                "xpath": f"{parent_path}/text()[{position}]"
            }
        return None
    
    # Build current XPath - much simpler and more reliable
    tag_count = 1
    if parent_path:
        # Count how many siblings of the same tag came before this one
        previous_siblings = []
        for sibling in element.previous_siblings:
            if hasattr(sibling, 'name') and sibling.name == element.name:
                tag_count += 1
    
    current_path = f"{parent_path}/{element.name}[{tag_count}]"
    
    # Add ID or class info for easier identification (but not in actual xpath)
    display_path = current_path
    if element.get('id'):
        display_path += f" (@id='{element['id']}')"
    elif element.get('class'):
        classes = ' '.join(element['class']) if isinstance(element['class'], list) else element['class']
        display_path += f" (@class='{classes}')"
    
    # Basic element info
    element_dict = {
        "type": "element",
        "tag": element.name,
        "xpath": current_path,
        "display_xpath": display_path,
        "attributes": dict(element.attrs) if element.attrs else {},
        "direct_text": element.get_text(separator=' ', strip=True) if element.string else "",
        "children": []
    }
    
    # Process special attributes
    if element.name == 'a' and element.get('href'):
        element_dict['attributes']['href'] = urljoin(base_url, element['href'])
        element_dict['link_text'] = element.get_text(strip=True)
    
    if element.name == 'img' and element.get('src'):
        element_dict['attributes']['src'] = urljoin(base_url, element['src'])
        element_dict['image_alt'] = element.get('alt', '')
    
    # Process children with proper positioning
    child_position = 1
    for child in element.children:
        if hasattr(child, 'name') or (child.string and child.string.strip()):
            child_dict = element_to_dict(child, base_url, current_path, child_position)
            if child_dict:
                element_dict['children'].append(child_dict)
                child_position += 1
    
    return element_dict

def create_flat_index(dom_tree: Dict[str, Any]) -> Dict[str, Any]:
    """Create a flat index of all elements for easy filtering"""
    index = {
        "by_tag": {},
        "by_class": {},
        "by_id": {},
        "by_xpath": {},
        "links": [],
        "images": [],
        "text_content": [],
        "headings": [],
        "tables": [],
        "forms": []
    }
    
    def traverse(element):
        if element.get('type') == 'text':
            if element.get('content') and len(element.get('content', '').strip()) > 3:
                index['text_content'].append({
                    "text": element['content'],
                    "xpath": element['xpath']
                })
            return
        
        if element.get('type') == 'element':
            tag = element.get('tag')
            xpath = element.get('xpath')
            
            # Index by tag
            if tag not in index['by_tag']:
                index['by_tag'][tag] = []
            index['by_tag'][tag].append(element)
            
            # Index by XPath
            if xpath:
                index['by_xpath'][xpath] = element
            
            # Index by class
            classes = element.get('attributes', {}).get('class')
            if classes:
                if isinstance(classes, list):
                    classes = ' '.join(classes)
                if classes not in index['by_class']:
                    index['by_class'][classes] = []
                index['by_class'][classes].append(element)
            
            # Index by ID
            element_id = element.get('attributes', {}).get('id')
            if element_id:
                index['by_id'][element_id] = element
            
            # Special content indexing
            if tag == 'a' and element.get('attributes', {}).get('href'):
                index['links'].append({
                    "text": element.get('link_text', ''),
                    "url": element['attributes']['href'],
                    "xpath": xpath
                })
            
            if tag == 'img':
                index['images'].append({
                    "src": element.get('attributes', {}).get('src', ''),
                    "alt": element.get('image_alt', ''),
                    "xpath": xpath
                })
            
            if tag in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
                index['headings'].append({
                    "text": element.get('direct_text', ''),
                    "level": int(tag[1]),
                    "xpath": xpath
                })
            
            if tag == 'table':
                index['tables'].append({
                    "xpath": xpath,
                    "element": element
                })
            
            if tag == 'form':
                index['forms'].append({
                    "xpath": xpath,
                    "action": element.get('attributes', {}).get('action', ''),
                    "method": element.get('attributes', {}).get('method', 'GET')
                })
            
            # Traverse children
            for child in element.get('children', []):
                traverse(child)
    
    traverse(dom_tree)
    return index

def simple_scrape(url: str) -> Dict[str, Any]:
    """Optimized scraping function with better performance"""
    try:
        # Use session for better performance
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        })
        
        response = session.get(url, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'lxml')  # lxml is faster than html.parser
        
        # Remove unwanted elements early to reduce processing
        for element in soup(["script", "style", "noscript", "meta", "link", "head"]):
            element.decompose()
        
        # Get root element
        root_element = soup.body if soup.body else soup.html
        if not root_element:
            root_element = soup
        
        # Build index directly without full DOM tree (much faster)
        index = build_optimized_index(root_element, url)
        
        # Meta information
        meta_info = {
            "url": url,
            "title": soup.title.string if soup.title else "No title",
            "meta_description": get_meta_description(soup),
            "scraped_at": str(uuid.uuid4())[:8]
        }
        
        return {
            "meta": meta_info,
            "index": index,
            "stats": {
                "total_elements": len(index['by_xpath']),
                "links_count": len(index['links']),
                "images_count": len(index['images']),
                "headings_count": len(index['headings']),
                "unique_tags": sorted(list(index['by_tag'].keys()))
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to scrape: {str(e)}")

def get_meta_description(soup):
    """Fast meta description extraction"""
    meta_desc = soup.find('meta', attrs={'name': 'description'})
    if meta_desc:
        return meta_desc.get('content', '')
    return None

def build_optimized_index(root_element, base_url: str) -> Dict[str, Any]:
    """Build index directly using CSS selectors (much faster than recursive traversal)"""
    index = {
        "by_tag": {},
        "by_class": {},
        "by_id": {},
        "by_xpath": {},
        "links": [],
        "images": [],
        "text_content": [],
        "headings": [],
        "tables": [],
        "forms": []
    }
    
    # Use CSS selectors for fast bulk extraction
    all_elements = root_element.find_all(True)  # Get all elements at once
    
    for i, element in enumerate(all_elements):
        if not element.name:  # Skip text nodes
            continue
            
        tag = element.name
        
        # Build XPath efficiently
        xpath = build_fast_xpath(element)
        
        # Create element dict with minimal processing
        element_dict = {
            "type": "element",
            "tag": tag,
            "xpath": xpath,
            "attributes": dict(element.attrs) if element.attrs else {},
            "text": element.get_text(strip=True)[:200] if element.get_text(strip=True) else "",  # Limit text length
        }
        
        # Index by tag
        if tag not in index['by_tag']:
            index['by_tag'][tag] = []
        index['by_tag'][tag].append(element_dict)
        
        # Index by XPath
        index['by_xpath'][xpath] = element_dict
        
        # Index by class (optimized)
        classes = element.get('class')
        if classes:
            class_str = ' '.join(classes) if isinstance(classes, list) else classes
            if class_str not in index['by_class']:
                index['by_class'][class_str] = []
            index['by_class'][class_str].append(element_dict)
        
        # Index by ID
        element_id = element.get('id')
        if element_id:
            index['by_id'][element_id] = element_dict
        
        # Specialized content extraction (only for relevant elements)
        if tag == 'a' and element.get('href'):
            href = urljoin(base_url, element['href'])
            index['links'].append({
                "text": element.get_text(strip=True)[:100],
                "url": href,
                "xpath": xpath
            })
        
        elif tag == 'img' and element.get('src'):
            src = urljoin(base_url, element['src'])
            index['images'].append({
                "src": src,
                "alt": element.get('alt', '')[:100],
                "xpath": xpath
            })
        
        elif tag in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
            text = element.get_text(strip=True)
            if text:
                index['headings'].append({
                    "text": text[:200],
                    "level": int(tag[1]),
                    "xpath": xpath
                })
        
        elif tag == 'table':
            index['tables'].append({
                "xpath": xpath,
                "rows": len(element.find_all('tr'))
            })
        
        elif tag == 'form':
            index['forms'].append({
                "xpath": xpath,
                "action": element.get('action', ''),
                "method": element.get('method', 'GET').upper()
            })
    
    # Extract text content efficiently using CSS selectors
    text_elements = root_element.find_all(['p', 'div', 'span'], string=True)
    for element in text_elements[:100]:  # Limit to first 100 text elements
        text = element.get_text(strip=True)
        if text and len(text) > 10:  # Filter meaningful text
            index['text_content'].append({
                "text": text[:300],  # Limit text length
                "xpath": build_fast_xpath(element)
            })
    
    return index

def build_fast_xpath(element) -> str:
    """Build XPath quickly without complex traversal"""
    components = []
    current = element
    
    while current and current.name:
        # Count position among siblings of same tag
        tag = current.name
        position = 1
        
        for sibling in current.previous_siblings:
            if hasattr(sibling, 'name') and sibling.name == tag:
                position += 1
        
        components.append(f"{tag}[{position}]")
        current = current.parent
        
        # Stop at body or html to avoid going too deep
        if current and current.name in ['body', 'html']:
            if current.name == 'body':
                components.append('body[1]')
            break
    
    components.reverse()
    return '/' + '/'.join(components) if components else '/unknown[1]'




@app.get("/api/{slug}/test-xpath/{test_xpath}")
async def test_xpath(slug: str, test_xpath: str):
    """Test XPath access"""
    if slug not in scraped_data:
        return {"error": "Xpath not found"}
    
    data = scraped_data[slug]
    element = data['index']['by_xpath'].get(test_xpath)
    
    return {
        "test_xpath": test_xpath,
        "found": element is not None,
        "element": element if element else None,
        "all_xpaths_starting_with_body": [
            xpath for xpath in data['index']['by_xpath'].keys() 
            if xpath.startswith('/body')
        ][:10]
    }


@app.get("/")
async def root():
    """Root endpoint that Railway health check can access"""
    return {
        "message": "ScrapeToAPI is running!", 
        "status": "healthy",
        "docs_url": "/docs" if os.getenv("DEBUG") else "Docs disabled in production"
    }

# Make sure your home page route comes after the root API endpoint
@app.get("/app", response_class=HTMLResponse)
async def home(request: Request):
    """Serve the main page with URL input form"""
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/scrape")
async def scrape_url(url: str = Form(...)):
    """Scrape URL and return API endpoint with caching"""
    url_str = str(url).strip()
    cache_key = get_cache_key(url_str)
    
    # Check cache first
    if cache_key in scrape_cache and is_cache_valid(scrape_cache[cache_key]):
        cached_data = scrape_cache[cache_key]
        slug = str(uuid.uuid4())[:8]
        scraped_data[slug] = cached_data['data']
        
        return JSONResponse({
            "success": True,
            "message": "URL scraped successfully! (cached)",
            "api_endpoint": f"/api/{slug}",
            "slug": slug,
            "cached": True,
            "preview": {
                "title": cached_data['data']['meta']['title'],
                "total_elements": cached_data['data']['stats']['total_elements'],
                "links_count": cached_data['data']['stats']['links_count'],
                "images_count": cached_data['data']['stats']['images_count'],
                "headings_count": cached_data['data']['stats']['headings_count'],
                "available_tags": cached_data['data']['stats']['unique_tags'][:15]
            }
        })
    
    slug = str(uuid.uuid4())[:8]
    
    try:
        # Scrape with optimized function
        start_time = time.time()
        data = simple_scrape(url_str)
        scrape_time = round(time.time() - start_time, 2)
        
        # Cache the result
        scrape_cache[cache_key] = {
            'data': data,
            'timestamp': time.time()
        }
        
        scraped_data[slug] = data
        
        return JSONResponse({
            "success": True,
            "message": f"URL scraped successfully in {scrape_time}s!",
            "api_endpoint": f"/api/{slug}",
            "slug": slug,
            "scrape_time": scrape_time,
            "cached": False,
            "preview": {
                "title": data['meta']['title'],
                "total_elements": data['stats']['total_elements'],
                "links_count": data['stats']['links_count'],
                "images_count": data['stats']['images_count'],
                "headings_count": data['stats']['headings_count'],
                "available_tags": data['stats']['unique_tags'][:15]
            }
        })
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Scraping failed: {str(e)}")

@app.get("/api/{slug}")
async def get_scraped_data(slug: str):
    """Get complete scraped data by slug"""
    if slug not in scraped_data:
        raise HTTPException(status_code=404, detail="Data not found")
    return scraped_data[slug]

@app.get("/api/{slug}/filter/tag/{tag_name}")
async def filter_by_tag(slug: str, tag_name: str):
    """Filter elements by HTML tag"""
    if slug not in scraped_data:
        raise HTTPException(status_code=404, detail="Data not found")
    
    data = scraped_data[slug]
    filtered_elements = data['index']['by_tag'].get(tag_name, [])
    
    return {
        "filter_type": "tag",
        "filter_value": tag_name,
        "count": len(filtered_elements),
        "elements": filtered_elements
    }



@app.get("/api/{slug}/filter/xpath")
async def filter_by_xpath(slug: str, xpath: str):
    """Filter elements by XPath using query parameter"""
    if slug not in scraped_data:
        raise HTTPException(status_code=404, detail="Data not found")
    
    print(f"Looking for XPath: '{xpath}'")  # Debug print
    
    data = scraped_data[slug]
    element = data['index']['by_xpath'].get(xpath)
    
    print(f"Found element: {element is not None}")  # Debug print
    
    if element:
        return {
            "filter_type": "xpath",
            "filter_value": xpath,
            "count": 1,
            "elements": [element]
        }
    else:
        return {
            "filter_type": "xpath", 
            "filter_value": xpath,
            "count": 0,
            "elements": [],
            "message": "No element found at this XPath",
            "searched_for": xpath,
            "available_xpaths_sample": list(data['index']['by_xpath'].keys())[:10]
        }


@app.get("/api/{slug}/browse")
async def browse_structure(slug: str):
    """Browse the DOM structure to find available XPaths"""
    if slug not in scraped_data:
        raise HTTPException(status_code=404, detail="Data not found")
    
    data = scraped_data[slug]
    
    def get_element_summary(element):
        """Get a summary of an element for browsing"""
        if element.get('type') == 'text':
            return {
                "xpath": element['xpath'],
                "type": "text",
                "content_preview": element['content'][:100] + "..." if len(element.get('content', '')) > 100 else element.get('content', '')
            }
        else:
            return {
                "xpath": element['xpath'],
                "type": "element", 
                "tag": element.get('tag'),
                "attributes": element.get('attributes', {}),
                "text_preview": element.get('direct_text', '')[:100] + "..." if len(element.get('direct_text', '')) > 100 else element.get('direct_text', ''),
                "children_count": len(element.get('children', []))
            }
    
    # Get all elements with their summaries
    all_elements = []
    for xpath, element in data['index']['by_xpath'].items():
        all_elements.append(get_element_summary(element))
    
    return {
        "total_elements": len(all_elements),
        "elements": all_elements
    }


@app.get("/api/{slug}/links")
async def get_links(slug: str):
    """Get all links from scraped data"""
    if slug not in scraped_data:
        raise HTTPException(status_code=404, detail="Data not found")
    return scraped_data[slug]['index']['links']

@app.get("/api/{slug}/images")
async def get_images(slug: str):
    """Get all images from scraped data"""
    if slug not in scraped_data:
        raise HTTPException(status_code=404, detail="Data not found")
    return scraped_data[slug]['index']['images']

@app.get("/api/{slug}/headings")
async def get_headings(slug: str):
    """Get all headings from scraped data"""
    if slug not in scraped_data:
        raise HTTPException(status_code=404, detail="Data not found")
    return scraped_data[slug]['index']['headings']

@app.get("/api/{slug}/text")
async def get_text_content(slug: str):
    """Get all text content from scraped data"""
    if slug not in scraped_data:
        raise HTTPException(status_code=404, detail="Data not found")
    return scraped_data[slug]['index']['text_content']

# Add this near the top of your endpoints, before other routes
@app.get("/health")
async def health_check():
    """Health check endpoint for deployment platforms"""
    return {
        "status": "healthy", 
        "service": "ScrapeToAPI",
        "timestamp": time.time()
    }

# @app.get("/")
# async def root():
#     """Root endpoint that Railway health check can access"""
#     return {
#         "message": "ScrapeToAPI is running!", 
#         "status": "healthy",
#         "docs_url": "/docs" if os.getenv("DEBUG") else "Docs disabled in production"
#     }

# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.0", port=8000)
