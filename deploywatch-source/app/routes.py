from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException

from flask import request, jsonify, render_template
from app import app
from app.models import add_student, mark_attendance, get_attendance, get_attendance_stats, get_student_attendance
from datetime import date, datetime

@app.route('/')
def home():
    return render_template("index.html")

@app.route('/register_student', methods=['POST'])
def register_student():
    data = request.get_json()
    name = data.get("name")

    if not name:
        return jsonify({"error": "Name is required"}), 400

    student_id = add_student(name)
    mark_attendance(student_id, str(date.today()), "Present")

    return jsonify({"message": f"Attendance recorded for {name}!"})

@app.route('/mark_absent', methods=['POST'])
def mark_absent():
    data = request.get_json()
    student_id = data.get("student_id")

    if not student_id:
        return jsonify({"error": "Student ID is required"}), 400

    mark_attendance(student_id, str(date.today()), "Absent")

    return jsonify({"message": "Student marked as Absent!"})

@app.route('/get_attendance', methods=['GET'])
def fetch_attendance():
    records = get_attendance()
    return render_template("attendance.html", records=records)

@app.route('/analytics')
def analytics_page():
    return render_template("analytics.html")

@app.route('/attendance_stats', methods=['GET'])
def attendance_stats():
    start_date = request.args.get('start_date', '2023-01-01')  # Default start date
    end_date = request.args.get('end_date', datetime.today().strftime('%Y-%m-%d'))
    student_name = request.args.get('student_name', None)  # Optional filter by student

    if student_name:
        stats = get_student_attendance(student_name, start_date, end_date)
    else:
        stats = get_attendance_stats(start_date, end_date)

    return jsonify(stats)


def get_k8s_clients():
    """Connect to Kubernetes using in-cluster credentials."""
    try:
        config.load_incluster_config()
    except ConfigException:
        config.load_kube_config()

    return client.AppsV1Api(), client.CoreV1Api()


@app.route('/api/cluster/overview')
def cluster_overview():
    try:
        apps_api, core_api = get_k8s_clients()

        deployments = apps_api.list_deployment_for_all_namespaces().items
        pods = core_api.list_pod_for_all_namespaces().items
        namespaces = core_api.list_namespace().items

        healthy = 0
        unhealthy = 0

        for deployment in deployments:
            desired = deployment.spec.replicas or 0
            ready = deployment.status.ready_replicas or 0

            if ready >= desired:
                healthy += 1
            else:
                unhealthy += 1

        pod_counts = {
            "running": 0,
            "pending": 0,
            "failed": 0,
            "succeeded": 0,
            "unknown": 0
        }

        for pod in pods:
            phase = (pod.status.phase or "Unknown").lower()
            if phase in pod_counts:
                pod_counts[phase] += 1
            else:
                pod_counts["unknown"] += 1

        return jsonify({
            "cluster": "Connected",
            "deployments": {
                "total": len(deployments),
                "healthy": healthy,
                "unhealthy": unhealthy
            },
            "pods": {
                "total": len(pods),
                **pod_counts
            },
            "namespaces": len(namespaces)
        })

    except Exception as error:
        app.logger.exception("Kubernetes overview API failed")
        return jsonify({
            "cluster": "Unavailable",
            "error": str(error)
        }), 503


@app.route('/api/deployments')
def list_deployments():
    try:
        apps_api, _ = get_k8s_clients()

        namespace = request.args.get('namespace')
        search = request.args.get('search', '').lower()

        if namespace and namespace != 'all':
            items = apps_api.list_namespaced_deployment(namespace).items
        else:
            items = apps_api.list_deployment_for_all_namespaces().items

        results = []

        for deployment in items:
            name = deployment.metadata.name
            ns = deployment.metadata.namespace

            if search and search not in name.lower():
                continue

            desired = deployment.spec.replicas or 0
            ready = deployment.status.ready_replicas or 0

            containers = deployment.spec.template.spec.containers
            images = [container.image for container in containers]

            results.append({
                "name": name,
                "namespace": ns,
                "images": images,
                "desired_replicas": desired,
                "ready_replicas": ready,
                "available_replicas": deployment.status.available_replicas or 0,
                "status": "Healthy" if ready >= desired else "Unhealthy"
            })

        return jsonify({"total": len(results), "deployments": results})

    except Exception as error:
        app.logger.exception("Deployment API failed")
        return jsonify({"error": str(error)}), 503


@app.route('/api/pods')
def list_pods():
    try:
        _, core_api = get_k8s_clients()

        namespace = request.args.get('namespace')
        search = request.args.get('search', '').lower()

        if namespace and namespace != 'all':
            items = core_api.list_namespaced_pod(namespace).items
        else:
            items = core_api.list_pod_for_all_namespaces().items

        results = []

        for pod in items:
            name = pod.metadata.name

            if search and search not in name.lower():
                continue

            containers = pod.spec.containers or []
            restart_count = sum(
                status.restart_count
                for status in (pod.status.container_statuses or [])
            )

            results.append({
                "name": name,
                "namespace": pod.metadata.namespace,
                "status": pod.status.phase or "Unknown",
                "restarts": restart_count,
                "node": pod.spec.node_name,
                "pod_ip": pod.status.pod_ip,
                "images": [container.image for container in containers]
            })

        return jsonify({"total": len(results), "pods": results})

    except Exception as error:
        app.logger.exception("Pod API failed")
        return jsonify({"error": str(error)}), 503


@app.route('/api/namespaces')
def list_namespaces():
    try:
        _, core_api = get_k8s_clients()
        items = core_api.list_namespace().items

        return jsonify({
            "total": len(items),
            "namespaces": [
                {
                    "name": ns.metadata.name,
                    "status": ns.status.phase
                }
                for ns in items
            ]
        })

    except Exception as error:
        app.logger.exception("Namespace API failed")
        return jsonify({"error": str(error)}), 503


@app.route('/api/events')
def list_events():
    try:
        _, core_api = get_k8s_clients()

        namespace = request.args.get('namespace')

        if namespace and namespace != 'all':
            items = core_api.list_namespaced_event(namespace).items
        else:
            items = core_api.list_event_for_all_namespaces().items

        results = []

        for event in sorted(
            items,
            key=lambda e: e.last_timestamp or e.event_time or e.first_timestamp,
            reverse=True
        ):
            results.append({
                "type": event.type,
                "reason": event.reason,
                "message": event.message,
                "object": event.involved_object.name,
                "namespace": event.metadata.namespace,
                "count": event.count,
                "time": str(
                    event.last_timestamp or event.event_time or event.first_timestamp
                )
            })

        return jsonify({
            "total": len(results),
            "events": results[:100]
        })

    except Exception as error:
        app.logger.exception("Events API failed")
        return jsonify({"error": str(error)}), 503
