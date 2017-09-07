"""Teamroom Tracking System.

Main logic and API routes.
"""

from flask import Flask, request, abort, Response
from database import get_db, init_db
from models import *
from functools import wraps
import json
from sqlalchemy.exc import IntegrityError
from sqlalchemy import or_
import datetime
import iso8601
from werkzeug.exceptions import HTTPException
import pytz

app = Flask(__name__)


def parse_datetime(date_string):
    try:
        date = iso8601.parse_date(date_string)
    except iso8601.ParseError:
        return None
    return date.astimezone(pytz.utc).replace(tzinfo=None)


def returns_json(f):
    """Decorator to add the content type to responses."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            r = f(*args, **kwargs)
        except HTTPException as e:
            # monkey-patch the headers / body to be json
            headers = e.get_headers()
            for header in headers:
                if 'Content-Type' in header:
                    headers.remove(header)
            headers.append(('Content-Type', 'application/json'))
            e.get_headers = lambda x: headers
            e.get_body = lambda x: json.dumps({"message": e.description})
            raise e
        if isinstance(r, tuple):
            return Response(r[0], status=r[1], content_type='application/json')
        else:
            return Response(r, content_type='application/json')
    return decorated_function


def includes_user(f):
    """Add a request user parameter to the decorated function."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'Authorization' not in request.headers or \
                not request.headers['Authorization'].startswith('Bearer '):
                    abort(401, "missing or invalid authorization token")
        else:
            token = request.headers['Authorization'][len('Bearer '):]
            u = User.verify_auth_token(token)
            if u is None:
                abort(401, "missing or invalid authorization token")
            else:
                return f(u, *args, **kwargs)
    return decorated_function


def json_param_exists(param_name, json_root=-1):
    """Check if the given parameter exists and is valid.

    If a json_root is included, check within that. Otherwise, use request.json.
    Checks:
    - Is the json_root "Truth-y" (not None, not blank, etc.)?
    - Is the parameter name in the json_root?
    - Is the value not None?
    """
    if json_root == -1:
        json_root = request.json
    return json_root and \
            param_name in json_root and \
            json_root[param_name] is not None


@app.teardown_appcontext
def shutdown_session(exception=None):
    """End the database session."""
    get_db().remove()


@app.route('/auth', methods=['POST'])
@returns_json
def auth():
    """Authenticate users."""
    if not json_param_exists('username'):
        abort(400, "one or more required parameter is missing")
    username = request.json['username']

    user = User.query.filter_by(name=username).first()

    if user is None:
        user = User(username, username + '@example.com')
        role = Role.query.filter_by(name='student').first()
        user.role = role
        team = Team(username)
        team_type = TeamType.query.filter_by(name='single').first()
        team.team_type = team_type
        team.members.append(user)
        get_db().add(user)
        get_db().add(team)
        get_db().commit()

    encoded = user.generate_auth_token()

    return json.dumps({'token': encoded})


@app.route('/user/<int:user_id>', methods=['GET'])
@returns_json
# TODO secure this?
def user_read(user_id):
    """Get a user by user ID."""
    user = User.query.get(user_id)

    if user is None:
        abort(404, "user not found")

    return json.dumps(user.as_dict(include_teams_and_permissions=True))


@app.route('/user', methods=['GET'])
@returns_json
def user_search_partial():
    """Get a user id from a partial user name."""
    username = request.args.get('search') or ''

    ret = []
    for user in User.query.filter(User.name.ilike(username + "%")):
        ret.append({
            "id": user.id,
            "name": user.name
            })
        return json.dumps(ret)


# team CRUD

@app.route('/team', methods=['POST'])
@returns_json
@includes_user
def team_add(token_user):
    """Add a team given a team name."""
    if not json_param_exists('name') or \
            not json_param_exists('type'):
                abort(400, "one or more required parameter is missing")
    name = request.json['name']
    team_type = TeamType.query.filter_by(name=request.json['type']).first()
    if not team_type:
        abort(400, "invalid team type")

    if team_type.name == 'other_team':
        if not token_user.has_permission('team.create') and \
                not token_user.has_permission('team.create.elevated'):
                    abort(403, 'team creation is not permitted')
    else:  # creating any team other than 'other_team' requires elevated
        if not token_user.has_permission('team.create.elevated'):
            abort(403, 'insufficient permissions to create a team of this type')

    team = Team(name=name)
    team.team_type = team_type

    try:
        get_db().add(team)
        get_db().commit()
    except IntegrityError:
        abort(409, 'team name is already in use')

    return '', 201


@app.route('/team/<int:team_id>', methods=['GET'])
@returns_json
@includes_user
def team_read(token_user, team_id):
    """Get a team's info."""
    team = Team.query.get(team_id)
    if team is None:
        abort(404, 'team not found')

    return json.dumps(team.as_dict(for_user=token_user))


@app.route('/team/<int:team_id>', methods=['PUT'])
@returns_json
@includes_user
def team_update(token_user, team_id):
    """Update a team's name given name."""
    team = Team.query.get(team_id)

    if team is None:
        abort(404, 'team not found')

    if not json_param_exists('name'):
        abort(400, 'one or more required parameter is missing')

    name = request.json['name']

    if not (token_user.has_permission('team.update.elevated') or
            (token_user.has_permission('team.update') and
                team.has_member(token_user))):
                abort(403, 'insufficient permissions to modify team')

    team.name = name

    try:
        get_db().add(team)
        get_db().commit()
    except IntegrityError:
        abort(409, 'team name is already in use')

    return '', 204


@app.route('/team/<int:team_id>', methods=['DELETE'])
@returns_json
@includes_user
def team_delete(token_user, team_id):
    """Delete a team given its ID."""
    team = Team.query.get(team_id)
    if team is None:
        abort(404, 'team not found')

    if team.team_type.name == 'single':
        abort(403, 'unable to delete team of type "single"')

    # check for permissions to delete the team
    if not (token_user.has_permission('team.delete.elevated') or
            (token_user.has_permission('team.delete') and
                team.has_member(token_user))):
                abort(403, 'insufficient permissions to delete team')

    # deschedule reservations for the team then delete the team
    Reservation.query.filter_by(team_id=team.id).delete()
    get_db().delete(team)
    get_db().commit()

    return '', 204


# add/remove user to team

@app.route('/team/<int:team_id>/user/<int:user_id>', methods=['POST'])
@returns_json
@includes_user
def team_user_add(token_user, team_id, user_id):
    """Add a user to a team given the team and user IDs."""
    team = Team.query.get(team_id)
    if team is None:
        abort(404, 'team not found')

    # check for permissions to update the team
    if not (token_user.has_permission('team.update.elevated') or
            (token_user.has_permission('team.update') and
                team.has_member(token_user))):
                abort(403, 'insufficient permissions to add user to team')

    # don't allow adding to 'single' teams
    if team.team_type == TeamType.query.filter_by(name='single').first():
        abort(400, 'cannot add a user to a "single" team')

    user = User.query.get(user_id)
    if user is None:
        abort(400, 'invalid user id')

    if team.has_member(user):
        abort(409, 'user already in team')

    user.teams.append(team)
    get_db().commit()

    return '', 201


@app.route('/team/<int:team_id>/user/<int:user_id>', methods=['DELETE'])
@returns_json
@includes_user
def team_user_delete(token_user, team_id, user_id):
    """Remove a user from a team given the team and user IDs."""
    team = Team.query.get(team_id)
    if team is None:
        abort(404, 'team not found')

    if len(team.members) == 1:
        abort(400, 'only one member on team -- use team delete instead')

    # check for permissions to delete the team
    if not (token_user.has_permission('team.update.elevated') or
            (token_user.has_permission('team.update') and
                team.has_member(token_user))):
                abort(403, 'insufficient permissions to delete user from team')

    user = User.query.get(user_id)
    if user is None:
        abort(400, 'invalid user id')

    user.teams.remove(team)
    get_db().commit()

    return '', 204


# reservation CRUD

@app.route('/reservation', methods=['POST'])
@returns_json
@includes_user
def reservation_add(token_user):
    """Add a reservation.

    Uses the team ID, room ID, created by ID, start and end date times.
    """
    if not json_param_exists('team_id') or \
            not json_param_exists('room_id') or \
            not json_param_exists('start') or \
            not json_param_exists('end'):
                abort(400, 'one or more required parameter is missing')

    team_id = request.json['team_id']
    team = Team.query.get(team_id)
    if team is None:
        abort(400, 'invalid team id')

    if not (token_user.has_permission('reservation.create') and team.has_member(token_user)):
        abort(403)

    room_id = request.json['room_id']
    room = Room.query.get(room_id)
    if room is None:
        abort(400, 'invalid room id')

    start = parse_datetime(request.json['start'])
    end = parse_datetime(request.json['end'])
    if start is None or end is None:
        abort(400, 'cannot parse start or end date')

    if start >= end:
        abort(400, "start time must be before end time")

    res = Reservation(team=team, room=room, created_by=token_user,
            start=start, end=end)

    attempt_override = False
    if json_param_exists("override") and isinstance(request.json["override"], bool):
        attempt_override = request.json["override"]

    conflict_status, conflicting_reservations = res.validate_conflicts()
    if conflict_status == Reservation.NO_CONFLICT:
        pass
    elif conflict_status == Reservation.CONFLICT_OVERRIDABLE:
        if attempt_override:
            # Delete conflicting reservations
            for conflict in conflicting_reservations:
                get_db().delete(conflict)
        else:
            return json.dumps({"overridable": True}), 409
    elif conflict_status == Reservation.CONFLICT_FAILURE:
        return json.dumps({"overridable": False}), 409

    get_db().add(res)
    get_db().commit()

    return '', 201


@app.route('/reservation/<int:res_id>', methods=['GET'])
@returns_json
@includes_user
def reservation_read(token_user, res_id):
    """Get a reservation's info given ID."""
    res = Reservation.query.get(res_id)
    if res is None:
        abort(404, 'reservation not found')

    return json.dumps(res.as_dict(for_user=token_user))


@app.route('/reservation/<int:res_id>', methods=['PUT'])
@returns_json
@includes_user
def reservation_update(token_user, res_id):
    """Update a reservation.

    Uses a room ID, start and end datetimes.
    """
    if not json_param_exists('room_id') or \
            not json_param_exists('start') or \
            not json_param_exists('end'):
                abort(400, 'one or more required parameter is missing')

    room_id = request.json['room_id']
    room = Room.query.get(room_id)
    if room is None:
        abort(400, 'invalid room id')

    start = parse_datetime(request.json['start'])
    end = parse_datetime(request.json['end'])
    if start is None or end is None:
        abort(400, 'cannot parse start or end date')

    res = Reservation.query.get(res_id)
    if res is None:
        abort(400, 'invalid reservation id')

    if not token_user.has_permission('reservation.update.elevated'):
        is_my_reservation = any(map(lambda m: m.id == token_user.id,
            res.team.members))
        if not (is_my_reservation and
                token_user.has_permission('reservation.update')):
            abort(403, 'insufficient permissions to update reservation')

    res.room = room
    res.start = start
    res.end = end

    attempt_override = False
    if json_param_exists("override") and isinstance(request.json["override"], bool):
        attempt_override = request.json["override"]

    conflict_status, conflicting_reservations = res.validate_conflicts()
    if conflict_status == Reservation.NO_CONFLICT:
        pass
    elif conflict_status == Reservation.CONFLICT_OVERRIDABLE:
        if attempt_override:
            # Delete conflicting reservations
            for conflict in conflicting_reservations:
                get_db().delete(conflict)
        else:
            return json.dumps({"overridable": True}), 409
    elif conflict_status == Reservation.CONFLICT_FAILURE:
        return json.dumps({"overridable": False}), 409

    get_db().commit()

    return '', 204


@app.route('/reservation/<int:res_id>', methods=['DELETE'])
@returns_json
@includes_user
def reservation_delete(token_user, res_id):
    """Remove a reservation given its ID."""
    res = Reservation.query.get(res_id)
    if res is None:
        abort(404, 'reservation not found')

    if not token_user.has_permission('reservation.delete.elevated'):
        is_my_reservation = any(map(lambda m: m.id == token_user.id,
            res.team.members))
        if not (is_my_reservation and
                token_user.has_permission('reservation.delete')):
            abort(403, 'insufficient permissions to delete reservation')

    get_db().delete(res)
    get_db().commit()

    return '', 204


# room CRUD

@app.route('/room', methods=['GET'])
@returns_json
def room_list():
    """List all rooms."""
    rooms = []
    for room in Room.query.all():
        rooms.append(room.as_dict())

    return json.dumps(rooms)


@app.route('/room', methods=['POST'])
@returns_json
# TODO secure this
def room_add():
    """Add a room, given the room number."""
    if not json_param_exists('number'):
        abort(400, 'invalid room number')

    if not isinstance(request.json['number'], str):
        abort(400, 'room number must be string')

    num = request.json['number']
    room = Room(number=num)

    try:
        get_db().add(room)
        get_db().commit()
    except IntegrityError:
        abort(409, 'room number is already in use')
    return json.dumps(room.as_dict(include_features=False)), 201


@app.route('/room/<int:room_id>', methods=['GET'])
@returns_json
def room_read(room_id):
    """Get a room's info given its ID."""
    room = Room.query.get(room_id)
    if room is None:
        abort(404, 'room not found')

    return json.dumps(room.as_dict(include_features=True))


@app.route('/room/<int:room_id>', methods=['PUT'])
@returns_json
# TODO secure this
def room_update(room_id):
    """Update a room given its room number and feature list."""
    room = Room.query.get(room_id)

    if room is None:
        abort(404, 'room not found')

    if not json_param_exists('number'):
        abort(400, 'invalid room number')

    number = request.json['number']
    room.number = number

    if not json_param_exists('features'):
        abort(400, 'one or more required parameter is missing')

    features = request.json['features']

    # remove relationships not in features
    for r in room.features:
        if r not in features:
            room.features.delete(r)

    # add relationships in features
    for f in features:
        if f not in room.features:
            room.features.add(f)

    get_db().commit()

    return '', 204


@app.route('/room/<int:room_id>', methods=['DELETE'])
@returns_json
# TODO secure this
def room_delete(room_id):
    """Remove a room given its ID."""
    room = Room.query.get(room_id)
    if room is None:
        abort(404, 'room not found')

    get_db().delete(room)
    get_db().commit()

    return '', 204


@app.route('/feature', methods=['GET'])
@returns_json
def feature_list():
    """List all rooms."""
    features = []
    for feature in RoomFeature.query.all():
        features.append(feature.as_dict())

    return json.dumps(features)


@app.route('/reservation', methods=['GET'])
@returns_json
def get_reservations():
    """Get a filtered reservation list.

    Optional query params: start, end
    """
    start_date = request.args.get('start')
    end_date = request.args.get('end')

    if start_date is not None and end_date is not None:
        start = parse_datetime(request.json['start'])
        end = parse_datetime(request.json['end'])
        if start is None or end is None:
            abort(400, 'cannot parse start or end date')

        reservations = Reservation.query.filter(
                Reservation.end >= start, Reservation.start <= end)
    else:
        reservations = Reservation.query.filter(
                or_(Reservation.start >= datetime.datetime.now(),
                    Reservation.end >= datetime.datetime.now()))

                reservations = map(lambda x: x.as_dict(), reservations)

    return json.dumps(reservations)


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'init':
        print 'init db...'
        init_db()

    import os
    if os.getenv('PRODUCTION') == 'TRUE':
        app.run(host='0.0.0.0')
    else:
        app.run()
