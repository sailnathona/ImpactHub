import os
import json
import random
import string
import datetime
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
from requests_oauthlib import OAuth1

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from flask_sqlalchemy import SQLAlchemy
from openai import OpenAI
from werkzeug.utils import secure_filename

###############################################
# SETUP & CONFIG
###############################################
# Use your real key or environment variable in production.
client = OpenAI(api_key="sk-proj-XXXXX")

app = Flask(__name__)
app.secret_key = "SUPERSECRETKEY"  # Replace with a secure key in production

base_dir = os.path.abspath(os.path.dirname(__file__))
db_path = os.path.join(base_dir, "campaigns.db")

app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + db_path
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

UPLOAD_FOLDER = os.path.join(base_dir, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

db = SQLAlchemy(app)

###############################################
# MODELS
###############################################
class Campaign(db.Model):
    id = db.Column(db.String(8), primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    start_date = db.Column(db.String(50), nullable=True)
    end_date = db.Column(db.String(50), nullable=True)

    round1_data = db.Column(db.Text, nullable=True)
    round2_data = db.Column(db.Text, nullable=True)
    round2_questions = db.Column(db.Text, nullable=True)

    progress_pct = db.Column(db.Integer, default=0)

    email_list = db.Column(db.Text, nullable=True)        # JSON array of addresses
    analytics_data = db.Column(db.Text, nullable=True)    # JSON object per recipient

    materials_json = db.Column(db.Text, nullable=True)    # e.g. { "files": [ ... ] }
    prompts_emails = db.Column(db.Text, nullable=True)    # e.g. [ "Email snippet 1", ... ]
    prompts_tweets = db.Column(db.Text, nullable=True)    # e.g. [ "Tweet snippet 1", ... ]

    campaign_plan = db.Column(db.Text, nullable=True)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not self.analytics_data:
            self.analytics_data = json.dumps({})
        if not self.email_list:
            self.email_list = json.dumps([])
        if not self.materials_json:
            self.materials_json = json.dumps({"files": []})
        if not self.prompts_emails:
            self.prompts_emails = json.dumps([])
        if not self.prompts_tweets:
            self.prompts_tweets = json.dumps([])

class EmailBotConfig(db.Model):
    """
    Stores the email sending configuration. We'll assume a single row with id=1 for simplicity.
    - method: "local" or "smtp"
    - smtp_host, smtp_port, smtp_user, smtp_pass, sender_email
    """
    id = db.Column(db.Integer, primary_key=True)
    method = db.Column(db.String(20), default="local")  # "local" or "smtp"
    smtp_host = db.Column(db.String(150), nullable=True)
    smtp_port = db.Column(db.String(10), nullable=True)
    smtp_user = db.Column(db.String(150), nullable=True)
    smtp_pass = db.Column(db.String(150), nullable=True)
    sender_email = db.Column(db.String(150), nullable=True)

class TwitterBotConfig(db.Model):
    """
    Stores Twitter credentials in JSON.
    We allow multiple rows, each with a 'name' and 'credentials_json'.
    credentials_json might store:
        {
          "API_KEY": "...",
          "BARER_TOKEN": "...",
          "API_SECRET_KEY": "...",
          "ACCESS_TOKEN": "...",
          "ACCESS_TOKEN_SECRET": "..."
        }
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    credentials_json = db.Column(db.Text, nullable=False)

###############################################
# JINJA FILTER: to fix 'loads' error
###############################################
@app.template_filter("json_loads")
def json_loads_filter(value):
    """
    Safely parse JSON string into Python object or return empty list/dict on error.
    """
    if not value:
        return []
    try:
        return json.loads(value)
    except:
        return []

###############################################
# HELPER: Update progress
###############################################
def update_progress_based_on_dates(campaign):
    if not campaign.start_date or not campaign.end_date:
        return
    try:
        import datetime
        s = datetime.datetime.strptime(campaign.start_date, "%Y-%m-%d").date()
        e = datetime.datetime.strptime(campaign.end_date, "%Y-%m-%d").date()
        t = datetime.date.today()
        if e <= s:
            return
        total_days = (e - s).days
        elapsed = (t - s).days
        if elapsed < 0:
            elapsed = 0
        if elapsed > total_days:
            elapsed = total_days
        fraction = elapsed / total_days
        campaign.progress_pct = int(fraction * 100)
    except ValueError:
        pass

###############################################
# PLACEHOLDER ROUTES for /dashboard /pull_all_contracts_ios
###############################################
@app.route("/dashboard")
def dashboard():
    """
    Placeholder route for /dashboard to avoid 404 errors.
    """
    return "Dashboard page placeholder."

@app.route("/pull_all_contracts_ios")
def pull_all_contracts_ios():
    """
    Placeholder route for /pull_all_contracts_ios to avoid 404 errors.
    """
    return jsonify({"status": "ok", "message": "No contracts found (placeholder)."})

###############################################
# ROUTES
###############################################
@app.route("/")
def main_page():
    return render_template("combined.html", page="main")

@app.route("/campaign_overview")
def campaign_overview():
    all_campaigns = Campaign.query.all()
    for c in all_campaigns:
        update_progress_based_on_dates(c)
    db.session.commit()
    return render_template("combined.html", page="campaign_overview", campaigns=all_campaigns)

@app.route("/create_campaign", methods=["GET","POST"])
def create_campaign():
    if request.method == "POST":
        c_goal = request.form.get("campaign_goal","")
        c_name = request.form.get("campaign_name","")
        start_type = request.form.get("start_date_type","")
        start_val = request.form.get("start_date","")
        end_type = request.form.get("end_date_type","")
        end_val = request.form.get("end_date","")
        
        s_date = resolve_date_input(start_type, start_val)
        e_date = resolve_date_input(end_type, end_val)
        obj = request.form.get("objective","")
        audience = request.form.get("target_audience","")

        cid = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        round1_json = {
            "campaign_goal": c_goal,
            "objective": obj,
            "target_audience": audience
        }

        new_c = Campaign(
            id=cid,
            name=c_name,
            start_date=s_date,
            end_date=e_date,
            round1_data=json.dumps(round1_json)
        )
        db.session.add(new_c)
        db.session.commit()

        q_json = get_additional_questions(round1_json)
        new_c.round2_questions = json.dumps(q_json)
        update_progress_based_on_dates(new_c)
        db.session.commit()

        return redirect(url_for("gpt_questions", campaign_id=cid))

    return render_template("combined.html", page="create_campaign")

@app.route("/gpt_questions/<campaign_id>", methods=["GET","POST"])
def gpt_questions(campaign_id):
    c = Campaign.query.get_or_404(campaign_id)
    update_progress_based_on_dates(c)
    db.session.commit()

    try:
        q_data = json.loads(c.round2_questions) or {}
    except:
        q_data = {"questions":[]}

    r1_dict = {}
    if c.round1_data:
        try:
            r1_dict = json.loads(c.round1_data)
        except:
            r1_dict = {}

    if request.method == "POST":
        answers = {}
        for q in q_data.get("questions", []):
            fld = q.get("field_name")
            if fld:
                ans = request.form.get(fld, "")
                if ans.strip().lower() in ["idk","no","none"]:
                    ans += " (Needs suggestions)"
                answers[fld] = ans

        c.round2_data = json.dumps(answers)
        db.session.commit()

        plan_md = generate_campaign_plan(r1_dict, answers)
        c.campaign_plan = plan_md
        db.session.commit()
        update_progress_based_on_dates(c)
        db.session.commit()

        return redirect(url_for("final_campaign_details", campaign_id=c.id))

    return render_template("combined.html",
                           page="gpt_questions",
                           questions=q_data.get("questions", []),
                           campaign=c,
                           r1_dict=r1_dict)

@app.route("/final_campaign_details/<campaign_id>")
def final_campaign_details(campaign_id):
    c = Campaign.query.get_or_404(campaign_id)
    r1 = json.loads(c.round1_data) if c.round1_data else {}
    r2 = json.loads(c.round2_data) if c.round2_data else {}

    update_progress_based_on_dates(c)
    db.session.commit()

    email_prompts = json.loads(c.prompts_emails) if c.prompts_emails else []
    tweet_prompts = json.loads(c.prompts_tweets) if c.prompts_tweets else []

    return render_template("combined.html",
                           page="final_campaign_details",
                           campaign=c,
                           r1=r1,
                           r2=r2,
                           emails=email_prompts,
                           tweets=tweet_prompts)

@app.route("/email_list/<campaign_id>", methods=["GET","POST"])
def email_list(campaign_id):
    c = Campaign.query.get_or_404(campaign_id)
    if request.method == "POST":
        raw = request.form.get("emails","")
        arr = [x.strip() for x in raw.replace(",", "\n").split("\n") if x.strip()]
        c.email_list = json.dumps(arr)

        analytics = {}
        for e in arr:
            analytics[e] = {"opened": False, "clicked": False}
        c.analytics_data = json.dumps(analytics)
        db.session.commit()

        update_progress_based_on_dates(c)
        db.session.commit()
        return redirect(url_for("send_emails_sim", campaign_id=c.id))

    return render_template("combined.html", page="email_list", campaign=c)

@app.route("/email_center")
def email_center():
    all_cs = Campaign.query.all()
    return render_template("combined.html", page="email_center", campaigns=all_cs)

@app.route("/send_emails_sim/<campaign_id>")
def send_emails_sim(campaign_id):
    c = Campaign.query.get_or_404(campaign_id)
    arr = json.loads(c.email_list) if c.email_list else []

    links_data = []
    for e in arr:
        o_link = url_for("track_open", campaign_id=c.id, email=e, _external=True)
        clink = url_for("track_click", campaign_id=c.id, email=e, _external=True)
        links_data.append({"email": e, "open_link": o_link, "click_link": clink})

    db.session.commit()
    update_progress_based_on_dates(c)
    db.session.commit()
    return render_template("combined.html",
                           page="send_emails_sim",
                           campaign=c,
                           links_data=links_data)

@app.route("/track_open/<campaign_id>/<path:email>")
def track_open(campaign_id, email):
    c = Campaign.query.get_or_404(campaign_id)
    ads = json.loads(c.analytics_data) if c.analytics_data else {}
    if email in ads:
        ads[email]["opened"] = True
    c.analytics_data = json.dumps(ads)
    db.session.commit()

    update_progress_based_on_dates(c)
    db.session.commit()
    return "Email opened (simulated). You may close this tab."

@app.route("/track_click/<campaign_id>/<path:email>")
def track_click(campaign_id, email):
    c = Campaign.query.get_or_404(campaign_id)
    ads = json.loads(c.analytics_data) if c.analytics_data else {}
    if email in ads:
        ads[email]["clicked"] = True
    c.analytics_data = json.dumps(ads)
    db.session.commit()

    update_progress_based_on_dates(c)
    db.session.commit()
    return "Pledge button clicked (simulated). You may close this tab."

@app.route("/analytics")
def analytics():
    all_c = Campaign.query.all()
    for cc in all_c:
        update_progress_based_on_dates(cc)
    db.session.commit()

    summary = []
    for cc in all_c:
        e_list = json.loads(cc.email_list) if cc.email_list else []
        ads = json.loads(cc.analytics_data) if cc.analytics_data else {}

        total_sent = len(e_list)
        opened = sum(1 for v in ads.values() if v["opened"]) if ads else 0
        clicked = sum(1 for v in ads.values() if v["clicked"]) if ads else 0

        summary.append({
            "id": cc.id,
            "name": cc.name,
            "total_sent": total_sent,
            "opened": opened,
            "clicked": clicked,
            "progress_pct": cc.progress_pct
        })

    return render_template("combined.html", page="analytics", summary=summary)

@app.route("/delete_campaign/<campaign_id>", methods=["POST"])
def delete_campaign(campaign_id):
    c = Campaign.query.get_or_404(campaign_id)
    db.session.delete(c)
    db.session.commit()
    flash("Campaign deleted successfully.", "success")
    return redirect(url_for("campaign_overview"))

###################################################
# AI FILL & SUGGEST
###################################################
@app.route("/ai_suggest", methods=["POST"])
def ai_suggest():
    data = request.json or {}
    campaign_goal = data.get("campaign_goal","")
    field_name = data.get("fieldName","")
    partial_data = data.get("partialData",{})
    typed_value = data.get("typedValue","")

    print(f"[SERVER] /ai_suggest => field={field_name}, typed_value='{typed_value}', campaign_goal='{campaign_goal}'")

    try:
        suggestions = ask_gpt_for_field_suggestions(
            campaign_goal, field_name, partial_data, typed_value
        )
        print(f"[SERVER] /ai_suggest => suggestions:\n{json.dumps(suggestions, indent=2)}")
        return jsonify({"status":"ok","suggestions":suggestions})
    except Exception as e:
        print("Error in /ai_suggest:", e)
        return jsonify({"status":"error","message":str(e)}), 500

@app.route("/ai_fill_all", methods=["POST"])
def ai_fill_all():
    data = request.json or {}
    user_goal = data.get("campaign_goal","")

    typed_name = data.get("typedCampaignName","")
    typed_obj = data.get("typedObjective","")
    typed_audience = data.get("typedAudience","")

    print(f"[SERVER] /ai_fill_all => user_goal='{user_goal}', typed_name='{typed_name}', typed_obj='{typed_obj}', typed_audience='{typed_audience}'")

    try:
        fields_map = {
            "campaign_name": typed_name,
            "objective": typed_obj,
            "target_audience": typed_audience
        }
        results = {}
        partial_data = {}

        for field_key, typedVal in fields_map.items():
            suggestions = ask_gpt_for_field_suggestions(user_goal, field_key, partial_data, typedVal)
            results[field_key] = suggestions

        print("[SERVER] /ai_fill_all => results:\n", json.dumps(results, indent=2))
        return jsonify({"status":"ok","data":results})
    except Exception as ex:
        print("Error in /ai_fill_all:", ex)
        return jsonify({"status":"error","message":str(ex)}), 500

@app.route("/ai_fill_all_round2", methods=["POST"])
def ai_fill_all_round2():
    data = request.json or {}
    campaign_goal = data.get("campaign_goal","")
    typed_answers = data.get("typedAnswers", {})

    print(f"[SERVER] /ai_fill_all_round2 => campaign_goal='{campaign_goal}', typed_answers:\n{json.dumps(typed_answers, indent=2)}")

    try:
        partial_data = {}
        results = {}
        for field_name, typed_value in typed_answers.items():
            suggestions = ask_gpt_for_field_suggestions(campaign_goal, field_name, partial_data, typed_value)
            results[field_name] = suggestions

        print("[SERVER] /ai_fill_all_round2 => results:\n", json.dumps(results, indent=2))
        return jsonify({"status":"ok","data":results})
    except Exception as ex:
        print("Error in /ai_fill_all_round2:", ex)
        return jsonify({"status":"error","message":str(ex)}), 500

def ask_gpt_for_field_suggestions(campaign_goal, field_name, partial_data, typed_value=""):
    field_instructions = {
        "campaign_name": "Generate short, catchy campaign name ideas matching the goal.",
        "objective": "Generate short objective statements describing the campaign’s aims.",
        "target_audience": "Generate short descriptions of who the campaign is targeting."
    }
    desc = field_instructions.get(field_name, "Generate short suggestions for this field.")

    system_msg = (
        "You are a helpful assistant generating short suggestions for one field. "
        "Each suggestion has 'text', 'tier' (Conservative, Realistic, Ambitious), and 'explanation'. "
        "Return valid JSON with a 'suggestions' array."
    )

    if typed_value.strip():
        user_text = (
            f"Field: '{field_name}'\n"
            f"{desc}\n\n"
            f"Campaign Goal: {campaign_goal}\n"
            f"Partial data: {json.dumps(partial_data, indent=2)}\n"
            f"The user typed partial text: '{typed_value}'\n"
            "Please produce 3 short suggestions, each with 'tier' and 'explanation'. "
            "Return them in JSON under 'suggestions'."
        )
    else:
        user_text = (
            f"Field: '{field_name}'\n"
            f"{desc}\n\n"
            f"Campaign Goal: {campaign_goal}\n"
            f"Partial data: {json.dumps(partial_data, indent=2)}\n"
            "No user typed text. Please produce 3 short suggestions, each with 'tier' and 'explanation'. "
            "Return them in JSON under 'suggestions'."
        )

    resp = client.chat.completions.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_text}
        ],
        temperature=0.7
    )
    raw = resp.choices[0].message.content.strip()
    print("[SERVER] GPT raw response:\n", raw)

    try:
        parsed = json.loads(raw)
        return parsed.get("suggestions", [])
    except json.JSONDecodeError:
        print("[SERVER] GPT returned invalid JSON => returning empty suggestions.\n", raw)
        return []

def get_additional_questions(round1_dict):
    system_msg = (
        "You are a helpful assistant collecting more info. "
        "The user gave Round 1 data. Produce clarifying Round 2 questions in JSON. "
        "No question about campaign duration."
    )
    user_msg = (
        f"Round 1 data:\n{json.dumps(round1_dict, indent=2)}\n"
        "Generate clarifying questions in JSON. e.g.\n"
        "{ \"questions\": [\n"
        "   {\"label\": \"Any constraints?\",\"type\":\"text\",\"field_name\":\"constraints\"}\n"
        " ]}"
    )
    try:
        r = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg}
            ],
            temperature=0.7
        )
        return json.loads(r.choices[0].message.content.strip())
    except Exception as e:
        print("Error generating second round questions:", e)
        return {"questions":[]}

def generate_campaign_plan(round1_dict, round2_dict):
    system_msg = (
        "You are an expert campaign strategist for a non-profit. "
        "Produce a final plan in Markdown from Round 1 & 2 data. Don't mention you're AI."
    )
    user_msg = (
        f"Round 1 data:\n{json.dumps(round1_dict, indent=2)}\n"
        f"Round 2 data:\n{json.dumps(round2_dict, indent=2)}\n"
        "Generate final plan in Markdown with styled sections."
    )
    try:
        r = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role":"system","content":system_msg},
                {"role":"user","content":user_msg}
            ],
            temperature=0.7
        )
        return r.choices[0].message.content.strip()
    except Exception as e:
        print("Error generating final campaign plan:", e)
        return "Error generating final campaign plan."

@app.route("/upload_materials/<campaign_id>", methods=["GET","POST"])
def upload_materials(campaign_id):
    c = Campaign.query.get_or_404(campaign_id)
    if request.method == "POST":
        file_list = []
        if "materials" in request.files:
            fls = request.files.getlist("materials")
            for f in fls:
                if f.filename:
                    fname = secure_filename(f.filename)
                    path = os.path.join(app.config["UPLOAD_FOLDER"], fname)
                    f.save(path)
                    file_list.append({"filename": fname, "path": path})

        existing = json.loads(c.materials_json)
        existing["files"].extend(file_list)
        c.materials_json = json.dumps(existing)
        db.session.commit()

        email_prompts = generate_prompts(file_list, prompt_type="email", count=50)
        tweet_prompts = generate_prompts(file_list, prompt_type="tweet", count=50)

        c.prompts_emails = json.dumps(email_prompts)
        c.prompts_tweets = json.dumps(tweet_prompts)
        db.session.commit()

        flash("Materials uploaded + prompts generated!", "success")
        return redirect(url_for("final_campaign_details", campaign_id=c.id))

    mat_info = json.loads(c.materials_json)
    return render_template("combined.html",
                           page="upload_materials",
                           campaign=c,
                           materials=mat_info.get("files", []))

def generate_prompts(file_list, prompt_type="email", count=50):
    filenames_text = ", ".join(f["filename"] for f in file_list)
    system_msg = f"You are a creative copywriter generating {prompt_type} ideas for a nonprofit campaign."
    user_msg = (
        f"Materials: {filenames_text}\n"
        f"Generate {count} short prompts or hooks for {prompt_type} messages.\n"
        "Return JSON like { \"prompts\": [...] }"
    )
    try:
        r = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role":"system","content":system_msg},
                {"role":"user","content":user_msg}
            ],
            temperature=0.7
        )
        raw = r.choices[0].message.content.strip()
        try:
            parsed = json.loads(raw)
            return parsed.get("prompts", [])
        except json.JSONDecodeError:
            print("[SERVER] GPT returned invalid JSON for prompts => returning [].\n", raw)
            return []
    except Exception as e:
        print("Error generating prompts:", e)
        return []

def send_tweet(prompt_text):
    print(f"Tweeting: {prompt_text} + [DocuSign Link] ...")

###################################################
# Separated generate emails/tweets
###################################################
@app.route("/ai_generate_emails/<campaign_id>", methods=["POST"])
def ai_generate_emails(campaign_id):
    c = Campaign.query.get_or_404(campaign_id)
    r1_dict = {}
    if c.round1_data:
        try:
            r1_dict = json.loads(c.round1_data)
        except:
            pass

    r2_dict = {}
    if c.round2_data:
        try:
            r2_dict = json.loads(c.round2_data)
        except:
            pass

    plan_text = c.campaign_plan or ""

    system_msg = (
        "You are a creative marketing copywriter. Generate a set of short, vivid newsletter paragraphs. "
        "Return JSON like:\n\n"
        "{\n"
        "  \"emails\": [\"Email snippet 1\", \"Email snippet 2\"]\n"
        "}\n\n"
        "Use the campaign info to highlight the goals, timeline, calls to action, etc."
    )

    user_msg = (
        f"Round1 Data:\n{json.dumps(r1_dict, indent=2)}\n"
        f"Round2 Data:\n{json.dumps(r2_dict, indent=2)}\n"
        f"Plan:\n{plan_text}\n"
        "Generate about 3-5 short newsletter email paragraphs. Provide JSON as described."
    )

    try:
        resp = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg}
            ],
            temperature=0.7
        )
        raw = resp.choices[0].message.content.strip()
        parsed = {}
        try:
            parsed = json.loads(raw)
        except:
            print("[SERVER] /ai_generate_emails => GPT returned invalid JSON, raw =\n", raw)
            parsed = {"emails": []}

        emails = parsed.get("emails", [])
        c.prompts_emails = json.dumps(emails)
        db.session.commit()

        flash("Generated email prompts successfully!", "success")
    except Exception as e:
        flash(f"Error generating email prompts: {e}", "danger")

    return redirect(url_for("final_campaign_details", campaign_id=c.id))

@app.route("/ai_generate_tweets/<campaign_id>", methods=["POST"])
def ai_generate_tweets(campaign_id):
    c = Campaign.query.get_or_404(campaign_id)
    r1_dict = {}
    if c.round1_data:
        try:
            r1_dict = json.loads(c.round1_data)
        except:
            pass

    r2_dict = {}
    if c.round2_data:
        try:
            r2_dict = json.loads(c.round2_data)
        except:
            pass

    plan_text = c.campaign_plan or ""

    system_msg = (
        "You are a creative marketing copywriter. Generate a set of short tweet lines for social media. "
        "Return JSON like:\n\n"
        "{\n"
        "  \"tweets\": [\"Tweet 1\", \"Tweet 2\"]\n"
        "}\n\n"
        "Use the campaign info to highlight the goals, timeline, calls to action, etc."
    )

    user_msg = (
        f"Round1 Data:\n{json.dumps(r1_dict, indent=2)}\n"
        f"Round2 Data:\n{json.dumps(r2_dict, indent=2)}\n"
        f"Plan:\n{plan_text}\n"
        "Generate about 3-5 short tweets. Provide JSON as described."
    )

    try:
        resp = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg}
            ],
            temperature=0.7
        )
        raw = resp.choices[0].message.content.strip()
        parsed = {}
        try:
            parsed = json.loads(raw)
        except:
            print("[SERVER] /ai_generate_tweets => GPT returned invalid JSON, raw =\n", raw)
            parsed = {"tweets": []}

        tweets = parsed.get("tweets", [])
        c.prompts_tweets = json.dumps(tweets)
        db.session.commit()

        flash("Generated tweet prompts successfully!", "success")
    except Exception as e:
        flash(f"Error generating tweet prompts: {e}", "danger")

    return redirect(url_for("final_campaign_details", campaign_id=c.id))

@app.route("/post_tweet/<campaign_id>", methods=["POST"])
def post_tweet(campaign_id):
    c = Campaign.query.get_or_404(campaign_id)
    tweet_text = request.form.get("tweet_text","").strip()
    if not tweet_text:
        flash("No tweet text provided.", "danger")
        return redirect(url_for("final_campaign_details", campaign_id=c.id))

    twconfigs = TwitterBotConfig.query.all()
    if not twconfigs:
        flash("No Twitter config found. Go to Settings to add your Twitter credentials.", "danger")
        return redirect(url_for("final_campaign_details", campaign_id=c.id))

    tw = twconfigs[0]
    try:
        creds = json.loads(tw.credentials_json)
    except:
        flash("Invalid Twitter credentials JSON in DB.", "danger")
        return redirect(url_for("final_campaign_details", campaign_id=c.id))

    try:
        API_KEY = creds.get("API_KEY","")
        API_SECRET_KEY = creds.get("API_SECRET_KEY","")
        ACCESS_TOKEN = creds.get("ACCESS_TOKEN","")
        ACCESS_TOKEN_SECRET = creds.get("ACCESS_TOKEN_SECRET","")

        auth = OAuth1(API_KEY, API_SECRET_KEY, ACCESS_TOKEN, ACCESS_TOKEN_SECRET)
        url = "https://api.twitter.com/2/tweets"
        payload = {"text": tweet_text}
        response = requests.post(url, json=payload, auth=auth)

        if response.status_code == 201:
            flash("Tweet posted successfully!", "success")
        else:
            flash(f"Failed to post tweet. Status code: {response.status_code}", "danger")
            flash(f"Response: {response.json()}", "danger")
    except Exception as ex:
        flash(f"Error calling Twitter API: {ex}", "danger")

    return redirect(url_for("final_campaign_details", campaign_id=c.id))

@app.route("/send_individual_email/<campaign_id>", methods=["POST"])
def send_individual_email(campaign_id):
    c = Campaign.query.get_or_404(campaign_id)
    snippet = request.form.get("email_body","").strip()
    if not snippet:
        flash("No email snippet provided.", "danger")
        return redirect(url_for("final_campaign_details", campaign_id=c.id))

    config = db.session.get(EmailBotConfig, 1)
    if not config:
        flash("No EmailBotConfig found; go to Settings to configure Email.", "danger")
        return redirect(url_for("final_campaign_details", campaign_id=c.id))

    recipients = json.loads(c.email_list) if c.email_list else []
    if not recipients:
        flash("No recipients found. Please set up the email list first.", "danger")
        return redirect(url_for("final_campaign_details", campaign_id=c.id))

    subject = f"Newsletter Snippet: {c.name}"
    body_text = snippet

    try:
        if config.method == "local":
            send_via_local_noauth(recipients, subject, body_text, config)
        else:
            send_via_smtp(recipients, subject, body_text, config)
        flash(f"Sent individual email snippet to {len(recipients)} recipients!", "success")
    except Exception as e:
        flash(f"Error sending email snippet: {e}", "danger")

    return redirect(url_for("final_campaign_details", campaign_id=c.id))

###################################################
# The missing route to send the combined newsletter
###################################################
@app.route("/send_newsletter_emails/<campaign_id>", methods=["POST"])
def send_newsletter_emails(campaign_id):
    """
    Sends the content in prompts_emails as one combined newsletter
    to each recipient in the campaign's email_list.
    """
    c = Campaign.query.get_or_404(campaign_id)
    config = db.session.get(EmailBotConfig, 1)
    if not config:
        flash("No EmailBotConfig found; go to Settings to configure Email.", "danger")
        return redirect(url_for("final_campaign_details", campaign_id=c.id))

    emails_list = json.loads(c.prompts_emails) if c.prompts_emails else []
    if not emails_list:
        flash("No prompts_emails found; generate email prompts first.", "danger")
        return redirect(url_for("final_campaign_details", campaign_id=c.id))

    recipients = json.loads(c.email_list) if c.email_list else []
    if not recipients:
        flash("No recipients found. Please set up the email list first.", "danger")
        return redirect(url_for("final_campaign_details", campaign_id=c.id))

    subject = f"Newsletter: {c.name}"
    body_text = "\n\n".join(emails_list)

    try:
        if config.method == "local":
            send_via_local_noauth(recipients, subject, body_text, config)
        else:
            send_via_smtp(recipients, subject, body_text, config)
        flash(f"Sent newsletter to {len(recipients)} recipients!", "success")
    except Exception as e:
        flash(f"Error sending emails: {e}", "danger")

    return redirect(url_for("final_campaign_details", campaign_id=c.id))

def send_via_local_noauth(recipients, subject, body_text, config):
    host = config.smtp_host or "localhost"
    # Use MailHog default port 1025
    port = int(config.smtp_port or 1025)
    from_addr = config.sender_email or "noreply@example.org"

    try:
        server = smtplib.SMTP(host, port)
    except Exception as conn_err:
        raise Exception(
            f"Could not connect to local mail server at {host}:{port}. "
            "Either run a local MTA (MailHog/Postfix/Sendmail) or update your settings."
        ) from conn_err

    for r in recipients:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = r
        part = MIMEText(body_text, "plain")
        msg.attach(part)
        server.sendmail(from_addr, r, msg.as_string())
    server.quit()

def send_via_smtp(recipients, subject, body_text, config):
    host = config.smtp_host
    port = int(config.smtp_port)
    user = config.smtp_user
    pw = config.smtp_pass
    from_addr = config.sender_email or user

    server = smtplib.SMTP(host, port)
    server.starttls()
    server.login(user, pw)
    for r in recipients:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = r
        part = MIMEText(body_text, "plain")
        msg.attach(part)
        server.sendmail(from_addr, r, msg.as_string())
    server.quit()

###############################################
# SETTINGS
###############################################
@app.route("/settings", methods=["GET","POST"])
def settings():
    email_config = db.session.get(EmailBotConfig, 1)
    if not email_config:
        email_config = EmailBotConfig(id=1, method="local")
        db.session.add(email_config)
        db.session.commit()

    if request.method == "POST":
        if "email_config_form" in request.form:
            method = request.form.get("method","local")
            smtp_host = request.form.get("smtp_host","")
            # Default to 1025 for MailHog
            smtp_port = request.form.get("smtp_port","1025")
            smtp_user = request.form.get("smtp_user","")
            smtp_pass = request.form.get("smtp_pass","")
            sender_email = request.form.get("sender_email","")

            email_config.method = method
            email_config.smtp_host = smtp_host
            email_config.smtp_port = smtp_port
            email_config.smtp_user = smtp_user
            email_config.smtp_pass = smtp_pass
            email_config.sender_email = sender_email
            db.session.commit()
            flash("Email settings updated!", "success")
            return redirect(url_for("settings"))

        if "twitter_config_form" in request.form:
            tname = request.form.get("tw_name","").strip()
            api_key = request.form.get("api_key","").strip()
            barer_token = request.form.get("barer_token","").strip()
            api_secret = request.form.get("api_secret_key","").strip()
            acc_token = request.form.get("access_token","").strip()
            acc_secret = request.form.get("access_token_secret","").strip()

            if not tname:
                flash("Twitter config name is required.", "danger")
                return redirect(url_for("settings"))

            creds = {
                "API_KEY": api_key,
                "BARER_TOKEN": barer_token,
                "API_SECRET_KEY": api_secret,
                "ACCESS_TOKEN": acc_token,
                "ACCESS_TOKEN_SECRET": acc_secret
            }
            j = json.dumps(creds)
            tw_new = TwitterBotConfig(name=tname, credentials_json=j)
            db.session.add(tw_new)
            db.session.commit()
            flash("New Twitter credential set added!", "success")
            return redirect(url_for("settings"))

    all_twitters = TwitterBotConfig.query.all()
    return render_template("combined.html",
                           page="settings",
                           config=email_config,
                           twitters=all_twitters)

@app.route("/delete_twitter_config/<int:tw_id>", methods=["POST"])
def delete_twitter_config(tw_id):
    twc = TwitterBotConfig.query.get_or_404(tw_id)
    db.session.delete(twc)
    db.session.commit()
    flash("Twitter config deleted!", "success")
    return redirect(url_for("settings"))

###############################################
# HELPER: Resolve date input
###############################################
def resolve_date_input(input_type, input_value):
    if not input_type:
        return ""
    if input_type == "exact":
        return input_value.strip()
    if ":" in input_value:
        parts = input_value.split(":")
        if len(parts) == 2:
            qty = int(parts[1])
            return date_from_offset(parts[0], qty)
    else:
        try:
            qty = int(input_value)
            return date_from_offset(input_type, qty)
        except:
            pass
    return ""

def date_from_offset(offset_type, offset_val):
    import datetime
    today = datetime.date.today()
    if offset_type == "days":
        fut = today + datetime.timedelta(days=offset_val)
        return fut.strftime("%Y-%m-%d")
    elif offset_type == "weeks":
        fut = today + datetime.timedelta(weeks=offset_val)
        return fut.strftime("%Y-%m-%d")
    elif offset_type == "months":
        # naive approach: 30 days
        fut = today + datetime.timedelta(days=30*offset_val)
        return fut.strftime("%Y-%m-%d")
    return ""

###############################################################################
# DOCUSIGN CODE (unchanged)
###############################################################################
DOCUSIGN_INTEGRATION_KEY = "0d2c0571-XXXXXXXXXXXXX"
DOCUSIGN_USER_ID = "a286ff7e-a682-XXXXXXXXXX"
RSA_PRIVATE_KEY = """-----BEGIN RSA PRIVATE KEY-----
==
-----END RSA PRIVATE KEY-----
"""
BASE_PATH = "https://demo.docusign.net/restapi"
OAUTH_HOST = "account-d.docusign.com"
SCOPES = ["signature", "impersonation"]

def generate_access_token():
    from docusign_esign import ApiClient
    api_client = ApiClient()
    api_client.set_base_path(BASE_PATH)
    try:
        token_response = api_client.request_jwt_user_token(
            client_id=DOCUSIGN_INTEGRATION_KEY,
            user_id=DOCUSIGN_USER_ID,
            private_key_bytes=RSA_PRIVATE_KEY.encode("utf-8"),
            expires_in=3600,
            scopes=SCOPES,
            oauth_host_name=OAUTH_HOST
        )
        return token_response.access_token
    except Exception as e:
        print("Error generating access token:", e)
        return None

@app.route('/send_to_docusign', methods=['POST'])
def send_to_docusign():
    """
    Example route demonstrating how you might send a PDF to DocuSign.
    Adjust as needed for your real 'ContractVersion' or similar model.
    """
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json
    if not data or 'version_id' not in data:
        return jsonify({"error": "Missing version_id"}), 400

    version_id = data['version_id']
    # TODO: load your contract version, build signers, etc...

    token = generate_access_token()
    if not token:
        return jsonify({"error":"Could not generate DocuSign access token."}), 500

    return jsonify({"status":"stub - must define ContractVersion model"}), 200

###############################################
# MAIN
###############################################
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        # Use db.session.get() to avoid LegacyAPIWarning
        row = db.session.get(EmailBotConfig, 1)
        if not row:
            # Default to method=local, pointing at MailHog on localhost:1025
            default_config = EmailBotConfig(
                id=1,
                method="local",
                smtp_host="localhost",
                smtp_port="1025",
                smtp_user="",
                smtp_pass="",
                sender_email="noreply@example.org"
            )
            db.session.add(default_config)
            db.session.commit()

    app.run(debug=True)