import os
import sys
import re
import hashlib
import requests
from bs4 import BeautifulSoup
import feedparser
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import pytz
from typing import List, Dict, Set, Tuple
import logging
from html import escape

# ========== 配置 ==========
MAX_ARTICLES_PER_SOURCE = 25   # 每个源最多取25条
BATCH_SIZE = 50                # 每批推送的最大条数
SUMMARY_MAX_LEN = 200          # 摘要最大长度（字符）

# 环境变量
MAIL_USER = os.getenv('MAIL_USER')
MAIL_PASS = os.getenv('MAIL_PASS')
MAIL_TO = os.getenv('MAIL_TO')
FEISHU_WEBHOOK = os.getenv('FEISHU_WEBHOOK')

# 时区
TZ = pytz.timezone('Asia/Shanghai')

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ========== 辅助函数 ==========
def get_beijing_time() -> str:
    """获取当前北京时间字符串"""
    now = datetime.now(TZ)
    return now.strftime("%Y-%m-%d %H:%M:%S")

def extract_article_text(html_content: str, url: str) -> str:
    """从HTML中提取正文"""
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 移除脚本和样式
        for script in soup(["script", "style"]):
            script.decompose()
        
        # 优先使用 article 标签
        article = soup.find('article')
        if article:
            text = article.get_text()
        else:
            # 尝试常见内容容器
            selectors = [
                'div.content', 
                'div.post-content', 
                'div.entry-content', 
                'main', 
                '.article-body',
                '.post-body',
                '.content-body'
            ]
            text = None
            for selector in selectors:
                container = soup.select_one(selector)
                if container:
                    text = container.get_text()
                    break
            if not text:
                text = soup.get_text()
        
        # 清理空白
        text = re.sub(r'\n\s*\n', '\n\n', text)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()
    except Exception as e:
        logger.error(f"提取正文失败 {url}: {e}")
        return ""

def summarize_content(content: str, url: str) -> str:
    """提取摘要：去除作者/编辑信息，保留前SUMMARY_MAX_LEN字符"""
    if not content:
        return "暂无摘要"
    
    # 去掉常见的作者/编辑行
    lines = content.split('\n')
    cleaned_lines = []
    for line in lines:
        line = line.strip()
        if re.match(r'^(作者|编辑|文/|来源|原标题|责编|记者|本文来源|原创|投稿)\s*[:：]', line, re.I):
            continue
        if line:
            cleaned_lines.append(line)
    
    cleaned = ' '.join(cleaned_lines)
    if len(cleaned) > SUMMARY_MAX_LEN:
        cleaned = cleaned[:SUMMARY_MAX_LEN] + '...'
    
    return cleaned if cleaned else "暂无摘要"

def fetch_from_html(url: str, source_name: str) -> List[Dict]:
    """抓取普通网页"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        resp = requests.get(url, timeout=15, headers=headers)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding
        html = resp.text
        
        text = extract_article_text(html, url)
        
        soup = BeautifulSoup(html, 'html.parser')
        title_tag = soup.find('title')
        title = title_tag.string.strip() if title_tag and title_tag.string else source_name
        title = title[:200]
        
        return [{
            'title': title,
            'link': url,
            'published': get_beijing_time(),
            'summary': text[:1000] if text else "",
            'source': source_name,
            'keywords': []
        }]
    except Exception as e:
        logger.error(f"抓取普通网页失败 {url}: {e}")
        return []

def fetch_rss(url: str, source_name: str, max_articles: int) -> List[Dict]:
    """抓取RSS源"""
    articles = []
    try:
        feed = feedparser.parse(url)
        
        if not feed.entries:
            return []
        
        for entry in feed.entries[:max_articles]:
            title = entry.get('title', '无标题')
            link = entry.get('link', '#')
            
            published = entry.get('published', entry.get('updated', ''))
            if published and isinstance(published, (int, float)):
                published = datetime.fromtimestamp(published, TZ).strftime("%Y-%m-%d %H:%M:%S")
            elif not published:
                published = get_beijing_time()
            
            summary = entry.get('summary', entry.get('description', ''))
            if summary:
                summary = re.sub(r'<[^>]+>', '', summary)
                summary = summary[:1000]
            else:
                summary = ""
            
            articles.append({
                'title': title,
                'link': link,
                'published': published,
                'summary': summary,
                'source': source_name,
                'keywords': []
            })
            
    except Exception as e:
        logger.error(f"抓取RSS失败 {source_name}: {e}")
    
    return articles

def load_rss_sources() -> List[Dict]:
    """从 rss_sources.txt 读取源"""
    sources = []
    file_path = 'rss_sources.txt'
    
    if not os.path.exists(file_path):
        logger.error(f"找不到配置文件: {file_path}")
        sys.exit(1)
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                
                parts = line.split('|')
                if len(parts) < 2:
                    logger.warning(f"第{line_num}行格式错误，跳过: {line}")
                    continue
                
                name = parts[0].strip()
                url = parts[1].strip()
                typ = parts[2].strip().lower() if len(parts) >= 3 else 'rss'
                
                sources.append({'name': name, 'url': url, 'type': typ})
    except Exception as e:
        logger.error(f"读取配置文件失败: {e}")
        sys.exit(1)
    
    if not sources:
        logger.error("没有有效的RSS源配置")
        sys.exit(1)
    
    logger.info(f"加载了 {len(sources)} 个信息源")
    return sources

def load_keywords() -> Set[str]:
    """从 keywords.txt 读取关键词"""
    keywords = set()
    file_path = 'keywords.txt'
    
    if not os.path.exists(file_path):
        logger.error(f"找不到关键词文件: {file_path}")
        sys.exit(1)
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip().lower()
                if line and not line.startswith('#'):
                    keywords.add(line)
    except Exception as e:
        logger.error(f"读取关键词文件失败: {e}")
        sys.exit(1)
    
    if not keywords:
        logger.error("没有有效关键词")
        sys.exit(1)
    
    logger.info(f"加载了 {len(keywords)} 个关键词")
    return keywords

def load_pushed_urls() -> Set[str]:
    """加载已推送的URL哈希集合"""
    try:
        with open('pushed_urls.txt', 'r', encoding='utf-8') as f:
            return set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        return set()

def save_pushed_urls(pushed_set: Set[str]):
    """保存已推送的URL哈希"""
    try:
        with open('pushed_urls.txt', 'w', encoding='utf-8') as f:
            for url_hash in pushed_set:
                f.write(url_hash + '\n')
    except Exception as e:
        logger.error(f"保存推送记录失败: {e}")

def fetch_articles() -> Tuple[List[Dict], Set[str]]:
    """抓取所有文章，并去重"""
    sources = load_rss_sources()
    keywords = load_keywords()
    pushed_urls = load_pushed_urls()
    new_pushed = set()
    all_articles = []
    
    logger.info(f"开始抓取 {len(sources)} 个源")
    
    for src in sources:
        logger.info(f"正在处理: {src['name']}")
        
        if src['type'] == 'html':
            articles = fetch_from_html(src['url'], src['name'])
        else:
            articles = fetch_rss(src['url'], src['name'], MAX_ARTICLES_PER_SOURCE)
        
        if not articles:
            continue
        
        matched_count = 0
        for art in articles:
            content_text = (art['title'] + ' ' + art['summary']).lower()
            matched = [kw for kw in keywords if kw in content_text]
            
            if not matched:
                continue
            
            url_hash = hashlib.md5(art['link'].encode()).hexdigest()
            if url_hash in pushed_urls:
                continue
            
            art['keywords'] = matched[:3]
            art['url_hash'] = url_hash
            
            all_articles.append(art)
            new_pushed.add(url_hash)
            matched_count += 1
        
        logger.info(f"  {src['name']} 匹配 {matched_count} 篇新文章")
    
    updated_pushed = pushed_urls.union(new_pushed)
    save_pushed_urls(updated_pushed)
    
    logger.info(f"总计抓取 {len(all_articles)} 篇新文章")
    return all_articles, new_pushed

def highlight_keywords(text: str, keywords: List[str], is_html: bool = True) -> str:
    """高亮关键词（红色字体）"""
    if not keywords:
        return text
    
    escaped_text = text
    for kw in keywords:
        if is_html:
            # HTML格式：红色加粗
            pattern = re.compile(re.escape(kw), re.IGNORECASE)
            escaped_text = pattern.sub(f'<span style="color: red; font-weight: bold;">{kw}</span>', escaped_text)
        else:
            # 飞书markdown格式：红色加粗（使用飞书支持的格式）
            pattern = re.compile(re.escape(kw), re.IGNORECASE)
            escaped_text = pattern.sub(f'**<font color="red">{kw}</font>**', escaped_text)
    
    return escaped_text

def format_message_html(articles: List[Dict]) -> str:
    """生成HTML格式的消息（用于邮件）"""
    if not articles:
        return "<p>今日暂无匹配关键词的资讯</p>"
    
    # 统计各源数量
    source_count = {}
    for art in articles:
        src = art['source']
        source_count[src] = source_count.get(src, 0) + 1
    
    # 统计关键词频率
    kw_freq = {}
    for art in articles:
        for kw in art['keywords']:
            kw_freq[kw] = kw_freq.get(kw, 0) + 1
    
    top_kws = sorted(kw_freq.items(), key=lambda x: x[1], reverse=True)[:5]
    top_kw_str = '、'.join([f"{kw}({cnt})" for kw, cnt in top_kws]) if top_kws else '无'
    
    # 构建HTML头部
    beijing_time = get_beijing_time()
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
                line-height: 1.6;
                color: #333;
                max-width: 800px;
                margin: 0 auto;
                padding: 20px;
            }}
            .header {{
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                padding: 20px;
                border-radius: 10px;
                margin-bottom: 30px;
            }}
            .stats {{
                background: #f8f9fa;
                padding: 15px;
                border-radius: 8px;
                margin-bottom: 20px;
                border-left: 4px solid #667eea;
            }}
            .article-card {{
                border: 2px solid #e0e0e0;
                border-radius: 12px;
                padding: 20px;
                margin-bottom: 20px;
                transition: transform 0.2s, box-shadow 0.2s;
            }}
            .article-card:hover {{
                transform: translateY(-2px);
                box-shadow: 0 4px 12px rgba(0,0,0,0.1);
            }}
            .card-blue {{
                border-color: #3b82f6;
                background: #eff6ff;
            }}
            .card-yellow {{
                border-color: #eab308;
                background: #fefce8;
            }}
            .article-title {{
                font-size: 18px;
                font-weight: bold;
                margin-bottom: 10px;
            }}
            .article-title a {{
                color: #1e40af;
                text-decoration: none;
            }}
            .article-title a:hover {{
                text-decoration: underline;
            }}
            .keywords {{
                margin: 10px 0;
                padding: 8px;
                background: white;
                border-radius: 6px;
                font-size: 14px;
            }}
            .summary {{
                color: #555;
                line-height: 1.6;
                margin-top: 10px;
            }}
            .source {{
                color: #6b7280;
                font-size: 12px;
                margin-top: 10px;
            }}
            .footer {{
                text-align: center;
                color: #9ca3af;
                font-size: 12px;
                margin-top: 30px;
                padding-top: 20px;
                border-top: 1px solid #e5e7eb;
            }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>📰 商业资讯摘要</h1>
            <p>{beijing_time}</p>
        </div>
        
        <div class="stats">
            <strong>📊 统计信息</strong><br>
            共计筛选出 <strong>{len(articles)}</strong> 条相关资讯<br>
            {', '.join([f"{src} {cnt}条" for src, cnt in source_count.items()])}<br>
            主要关键词：{top_kw_str}
        </div>
    """
    
    # 按来源分组
    by_source = {}
    for art in articles:
        by_source.setdefault(art['source'], []).append(art)
    
    # 全局序号
    global_idx = 1
    for src, items in by_source.items():
        html += f'<h2 style="color: #374151; border-bottom: 2px solid #e5e7eb; padding-bottom: 8px;">📌 {src}（{len(items)}条）</h2>\n'
        
        for idx, art in enumerate(items, 1):
            # 交替使用蓝色和黄色边框
            card_class = "card-blue" if global_idx % 2 == 1 else "card-yellow"
            
            # 高亮标题中的关键词
            title_html = highlight_keywords(art['title'], art['keywords'], is_html=True)
            
            # 高亮摘要中的关键词
            summary_html = highlight_keywords(summarize_content(art['summary'], art['link']), art['keywords'], is_html=True)
            
            html += f"""
            <div class="article-card {card_class}">
                <div class="article-title">
                    {global_idx}. <a href="{art['link']}" target="_blank">{title_html}</a>
                </div>
                <div class="keywords">
                    🔑 关键词：<span style="color: red; font-weight: bold;">{'、'.join(art['keywords'])}</span>
                </div>
                <div class="summary">
                    📝 {summary_html}
                </div>
                <div class="source">
                    📍 来源：{art['source']} | 🕐 {art['published']}
                </div>
            </div>
            """
            global_idx += 1
    
    html += f"""
        <div class="footer">
            推送时间：{beijing_time}<br>
            本邮件由自动资讯系统生成
        </div>
    </body>
    </html>
    """
    
    return html

def format_message_feishu(articles: List[Dict]) -> str:
    """生成飞书Markdown格式的消息"""
    if not articles:
        return "今日暂无匹配关键词的资讯"
    
    # 统计各源数量
    source_count = {}
    for art in articles:
        src = art['source']
        source_count[src] = source_count.get(src, 0) + 1
    
    # 统计关键词频率
    kw_freq = {}
    for art in articles:
        for kw in art['keywords']:
            kw_freq[kw] = kw_freq.get(kw, 0) + 1
    
    top_kws = sorted(kw_freq.items(), key=lambda x: x[1], reverse=True)[:5]
    top_kw_str = '、'.join([f"{kw}({cnt})" for kw, cnt in top_kws]) if top_kws else '无'
    
    # 构建消息头部
    beijing_time = get_beijing_time()
    msg = f"""📰 **商业资讯摘要** ({beijing_time})

📊 **统计信息**
共计筛选出 **{len(articles)}** 条相关资讯
{', '.join([f"{src} {cnt}条" for src, cnt in source_count.items()])}
主要关键词：{top_kw_str}

"""
    
    # 按来源分组
    by_source = {}
    for art in articles:
        by_source.setdefault(art['source'], []).append(art)
    
    # 全局序号
    global_idx = 1
    for src, items in by_source.items():
        msg += f"\n**📌 {src}（{len(items)}条）**\n\n"
        
        for art in items:
            # 交替使用不同样式的分隔线
            if global_idx % 2 == 1:
                msg += f"🔵 **{global_idx}. [{art['title']}]({art['link']})**\n"
            else:
                msg += f"🟡 **{global_idx}. [{art['title']}]({art['link']})**\n"
            
            # 关键词显示（红色高亮）
            kw_display = []
            for kw in art['keywords']:
                kw_display.append(f"<font color='red'>{kw}</font>")
            msg += f"🔑 关键词：{', '.join(kw_display)}\n"
            
            # 摘要（高亮关键词）
            summary = summarize_content(art['summary'], art['link'])
            for kw in art['keywords']:
                # 飞书支持的部分HTML标签
                summary = re.sub(re.escape(kw), f"<font color='red'>{kw}</font>", summary, flags=re.IGNORECASE)
            msg += f"📝 {summary}\n"
            
            msg += f"📍 来源：{art['source']} | 🕐 {art['published']}\n\n"
            
            global_idx += 1
    
    msg += f"\n---\n推送时间：{beijing_time}"
    return msg

def send_email_html(content_html: str):
    """发送HTML格式邮件"""
    if not all([MAIL_USER, MAIL_PASS, MAIL_TO]):
        logger.warning("邮件配置不完整，跳过")
        return
    
    try:
        to_list = [email.strip() for email in MAIL_TO.split(',')]
        
        # 创建HTML邮件
        msg = MIMEMultipart('alternative')
        msg['From'] = MAIL_USER
        msg['To'] = ', '.join(to_list)
        msg['Subject'] = f"📰 商业资讯摘要 {datetime.now(TZ).strftime('%Y-%m-%d')}"
        
        # 创建纯文本版本（备用）
        text_version = re.sub(r'<[^>]+>', '', content_html)
        text_part = MIMEText(text_version, 'plain', 'utf-8')
        
        # HTML版本
        html_part = MIMEText(content_html, 'html', 'utf-8')
        
        msg.attach(text_part)
        msg.attach(html_part)
        
        server = smtplib.SMTP_SSL('smtp.qq.com', 465)
        server.login(MAIL_USER, MAIL_PASS)
        server.send_message(msg)
        server.quit()
        
        logger.info(f"邮件发送成功，收件人: {MAIL_TO}")
        
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")

def send_feishu(content: str):
    """推送飞书"""
    if not FEISHU_WEBHOOK:
        logger.warning("飞书未配置，跳过")
        return
    
    try:
        # 飞书消息限制，如果内容过长则分批
        max_length = 30000
        if len(content) > max_length:
            logger.warning(f"内容过长({len(content)}字符)，将截断")
            content = content[:max_length] + "\n...(内容过长已截断)"
        
        payload = {
            "msg_type": "interactive",
            "card": {
                "config": {
                    "wide_screen_mode": True
                },
                "header": {
                    "title": {
                        "content": f"📰 商业资讯摘要 {datetime.now(TZ).strftime('%m-%d')}",
                        "tag": "plain_text"
                    },
                    "template": "blue"
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": content
                    }
                ]
            }
        }
        
        resp = requests.post(FEISHU_WEBHOOK, json=payload, timeout=10)
        
        if resp.status_code == 200:
            result = resp.json()
            if result.get('code') == 0:
                logger.info("飞书推送成功")
            else:
                logger.error(f"飞书推送失败: {result}")
        else:
            logger.error(f"飞书推送HTTP错误: {resp.status_code}")
            
    except Exception as e:
        logger.error(f"飞书推送异常: {e}")

def main():
    """主函数"""
    logger.info("="*50)
    logger.info(f"开始执行推送任务 - {get_beijing_time()}")
    logger.info("="*50)
    
    try:
        # 抓取文章
        articles, new_pushed = fetch_articles()
        logger.info(f"本次新文章数量: {len(articles)}")
        
        if not articles:
            logger.info("没有新文章，结束任务")
            return
        
        # 生成HTML格式邮件
        html_msg = format_message_html(articles)
        
        # 生成飞书格式消息
        feishu_msg = format_message_feishu(articles)
        
        # 分批次推送
        if len(articles) > BATCH_SIZE:
            logger.info(f"文章超过{BATCH_SIZE}条，分批次推送")
            
            # 邮件分批（HTML格式）
            for i in range(0, len(articles), BATCH_SIZE):
                batch_articles = articles[i:i+BATCH_SIZE]
                batch_html = format_message_html(batch_articles)
                send_email_html(batch_html)
                logger.info(f"已发送第 {i//BATCH_SIZE + 1} 批邮件")
            
            # 飞书分批（Markdown格式）
            for i in range(0, len(articles), BATCH_SIZE):
                batch_articles = articles[i:i+BATCH_SIZE]
                batch_feishu = format_message_feishu(batch_articles)
                send_feishu(batch_feishu)
                logger.info(f"已推送第 {i//BATCH_SIZE + 1} 批飞书")
        else:
            send_email_html(html_msg)
            send_feishu(feishu_msg)
        
        logger.info("任务执行完成")
        
    except Exception as e:
        logger.error(f"任务执行失败: {e}", exc_info=True)
        sys.exit(1)
    
    logger.info("="*50)

if __name__ == "__main__":
    main()
