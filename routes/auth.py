"""Auth routes: login, logout."""
from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_user, logout_user, login_required, current_user
from flask_bcrypt import check_password_hash as _check_pw
from sqlalchemy import select

from db.core import get_session
from db.newsroom import AdminUser

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    session = get_session()
    user = session.execute(
        select(AdminUser).where(AdminUser.username == username)
    ).scalar_one_or_none()
    session.close()

    if user and _check_pw(user.password_hash, password):
        login_user(user)
        next_page = request.args.get("next")
        return redirect(next_page or url_for("admin.dashboard"))

    flash("Invalid username or password.", "error")
    return render_template("login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
