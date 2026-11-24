/**
 * TravelGuardian 3D Transatlantic Flight Visualizer (Three.js)
 * High-craft aeronautical visualization with realistic great-circle 3D arcs,
 * waypoint beacons, and dynamic flight state transitions.
 */

class FlightVisualizer3D {
    constructor(containerId) {
        this.container = document.getElementById(containerId);
        if (!this.container) return;

        this.width = this.container.clientWidth;
        this.height = this.container.clientHeight || 240;
        this.state = 'ontime'; // 'ontime' | 'delayed' | 'resolving' | 'approved' | 'declined'
        this.selectedOption = 'UA938';

        this.init();
    }

    init() {
        // Scene setup
        this.scene = new THREE.Scene();
        this.scene.background = new THREE.Color(0xF6F2EA);

        // Camera setup (perspective with narrow FOV for isometric/ortho feel)
        this.camera = new THREE.PerspectiveCamera(38, this.width / this.height, 1, 1000);
        this.camera.position.set(0, 35, 160);

        // Renderer setup
        this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
        this.renderer.setSize(this.width, this.height);
        this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
        this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
        this.renderer.toneMappingExposure = 1.1;
        this.container.appendChild(this.renderer.domElement);

        // Lighting
        const ambientLight = new THREE.AmbientLight(0xFFFFFF, 0.85);
        this.scene.add(ambientLight);

        const dirLight = new THREE.DirectionalLight(0xFDF8EE, 0.9);
        dirLight.position.set(40, 80, 70);
        this.scene.add(dirLight);

        const backLight = new THREE.DirectionalLight(0xB88A3B, 0.4);
        backLight.position.set(-60, -30, -50);
        this.scene.add(backLight);

        // Sphere / Earth Segment
        this.globeRadius = 55;
        this.createGlobe();

        // City Waypoints (Lat, Lng)
        this.airports = {
            SFO: { name: 'SFO', lat: 37.6213, lng: -122.3790, color: 0x14213D },
            ORD: { name: 'ORD', lat: 41.9742, lng: -87.9073, color: 0xB88A3B },
            LHR: { name: 'LHR', lat: 51.4700, lng: -0.4543, color: 0x14213D }
        };

        this.airportPositions = {};
        for (const [code, info] of Object.entries(this.airports)) {
            this.airportPositions[code] = this.latLngToVector3(info.lat, info.lng, this.globeRadius);
        }

        this.createWaypoints();
        this.createFlightArcs();
        this.createAircrafts();

        // Damped Mouse Interaction / Parallax
        this.mouse = { x: 0, y: 0, targetX: 0, targetY: 0 };
        this.isDragging = false;
        this.previousMousePosition = { x: 0, y: 0 };

        this.setupEventListeners();

        // Animation Loop
        this.clock = new THREE.Clock();
        this.animate = this.animate.bind(this);
        requestAnimationFrame(this.animate);
    }

    latLngToVector3(lat, lng, radius) {
        // Rotate globe slightly so North America and Europe are centered nicely
        const phi = (90 - lat) * (Math.PI / 180);
        const theta = (lng + 70) * (Math.PI / 180);

        const x = -(radius * Math.sin(phi) * Math.cos(theta));
        const z = radius * Math.sin(phi) * Math.sin(theta);
        const y = radius * Math.cos(phi);

        return new THREE.Vector3(x, y, z);
    }

    createGlobe() {
        this.globeGroup = new THREE.Group();
        this.scene.add(this.globeGroup);

        // Core Parchment Sphere
        const sphereGeo = new THREE.SphereGeometry(this.globeRadius, 48, 48);
        const sphereMat = new THREE.MeshStandardMaterial({
            color: 0xFAF8F3,
            roughness: 0.9,
            metalness: 0.05,
        });
        const sphere = new THREE.Mesh(sphereGeo, sphereMat);
        this.globeGroup.add(sphere);

        // Graticule / Latitude & Longitude Navigation Rings
        const graticuleMat = new THREE.LineBasicMaterial({
            color: 0xB88A3B,
            transparent: true,
            opacity: 0.16
        });

        // Latitude circles
        for (let lat = 20; lat <= 70; lat += 15) {
            const rad = this.globeRadius * Math.cos(lat * Math.PI / 180);
            const y = this.globeRadius * Math.sin(lat * Math.PI / 180);
            const ringGeo = new THREE.BufferGeometry();
            const points = [];
            for (let i = 0; i <= 64; i++) {
                const theta = (i / 64) * Math.PI * 2;
                points.push(new THREE.Vector3(rad * Math.cos(theta), y, rad * Math.sin(theta)));
            }
            ringGeo.setFromPoints(points);
            const line = new THREE.Line(ringGeo, graticuleMat);
            this.globeGroup.add(line);
        }

        // Longitude meridian arcs
        for (let lng = -130; lng <= 20; lng += 25) {
            const ringGeo = new THREE.BufferGeometry();
            const points = [];
            const theta = (lng + 70) * (Math.PI / 180);
            for (let lat = 10; lat <= 80; lat += 2) {
                const phi = (90 - lat) * (Math.PI / 180);
                const x = -(this.globeRadius * Math.sin(phi) * Math.cos(theta));
                const z = this.globeRadius * Math.sin(phi) * Math.sin(theta);
                const y = this.globeRadius * Math.cos(phi);
                points.push(new THREE.Vector3(x, y, z));
            }
            ringGeo.setFromPoints(points);
            const line = new THREE.Line(ringGeo, graticuleMat);
            this.globeGroup.add(line);
        }
    }

    createWaypoints() {
        this.waypointObjects = {};

        for (const [code, info] of Object.entries(this.airports)) {
            const pos = this.airportPositions[code];
            const group = new THREE.Group();
            group.position.copy(pos);

            // Align group normal to sphere surface
            group.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), pos.clone().normalize());

            // Base Disc
            const discGeo = new THREE.CylinderGeometry(1.6, 1.6, 0.3, 24);
            const discMat = new THREE.MeshStandardMaterial({
                color: info.color,
                roughness: 0.3,
                metalness: 0.8
            });
            const disc = new THREE.Mesh(discGeo, discMat);
            group.add(disc);

            // Glowing Outer Ring
            const ringGeo = new THREE.RingGeometry(1.9, 2.5, 32);
            ringGeo.rotateX(-Math.PI / 2);
            const ringMat = new THREE.MeshBasicMaterial({
                color: info.color,
                side: THREE.DoubleSide,
                transparent: true,
                opacity: 0.7
            });
            const ring = new THREE.Mesh(ringGeo, ringMat);
            group.add(ring);

            // Pulse Ring for ORD Disruption
            if (code === 'ORD') {
                const pulseGeo = new THREE.RingGeometry(2.6, 3.4, 32);
                pulseGeo.rotateX(-Math.PI / 2);
                this.ordPulseMat = new THREE.MeshBasicMaterial({
                    color: 0xB6382E,
                    side: THREE.DoubleSide,
                    transparent: true,
                    opacity: 0.0
                });
                this.ordPulseMesh = new THREE.Mesh(pulseGeo, this.ordPulseMat);
                group.add(this.ordPulseMesh);
                this.ordRingMat = ringMat;
                this.ordDiscMat = discMat;
            }

            // Airport Label Sprite
            const sprite = this.createAirportLabelSprite(code);
            sprite.position.set(0, 4.5, 0);
            sprite.scale.set(7.5, 3.8, 1);
            group.add(sprite);

            this.globeGroup.add(group);
            this.waypointObjects[code] = group;
        }
    }

    createAirportLabelSprite(text) {
        const canvas = document.createElement('canvas');
        canvas.width = 128;
        canvas.height = 64;
        const ctx = canvas.getContext('2d');

        ctx.fillStyle = 'rgba(252, 251, 248, 0.95)';
        ctx.strokeStyle = '#E3DACB';
        ctx.lineWidth = 2;

        // Rounded rect
        ctx.beginPath();
        ctx.roundRect(8, 8, 112, 48, 8);
        ctx.fill();
        ctx.stroke();

        ctx.font = 'bold 22px "JetBrains Mono", monospace';
        ctx.fillStyle = '#14213D';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(text, 64, 32);

        const texture = new THREE.CanvasTexture(canvas);
        const spriteMat = new THREE.SpriteMaterial({ map: texture, depthTest: false });
        return new THREE.Sprite(spriteMat);
    }

    createGreatCircleArc(start, end, maxHeight = 10, numPoints = 80) {
        const points = [];
        for (let i = 0; i <= numPoints; i++) {
            const t = i / numPoints;
            // Slerp on unit sphere
            const p = new THREE.Vector3().copy(start).lerp(end, t);
            const factor = Math.sin(t * Math.PI);
            p.normalize().multiplyScalar(this.globeRadius + factor * maxHeight);
            points.push(p);
        }
        return points;
    }

    createFlightArcs() {
        // Leg 1: SFO -> ORD
        const ptsSfoOrd = this.createGreatCircleArc(this.airportPositions.SFO, this.airportPositions.ORD, 6.5);
        this.curveSfoOrd = new THREE.CatmullRomCurve3(ptsSfoOrd);
        const geoSfoOrd = new THREE.BufferGeometry().setFromPoints(ptsSfoOrd);
        this.matSfoOrd = new THREE.LineBasicMaterial({ color: 0xB88A3B, linewidth: 2.5 });
        this.lineSfoOrd = new THREE.Line(geoSfoOrd, this.matSfoOrd);
        this.globeGroup.add(this.lineSfoOrd);

        // Leg 2: ORD -> LHR (Original On-Time)
        const ptsOrdLhr = this.createGreatCircleArc(this.airportPositions.ORD, this.airportPositions.LHR, 11);
        this.curveOrdLhr = new THREE.CatmullRomCurve3(ptsOrdLhr);
        const geoOrdLhr = new THREE.BufferGeometry().setFromPoints(ptsOrdLhr);
        this.matOrdLhr = new THREE.LineBasicMaterial({ color: 0xB88A3B, linewidth: 2.5 });
        this.lineOrdLhr = new THREE.Line(geoOrdLhr, this.matOrdLhr);
        this.globeGroup.add(this.lineOrdLhr);

        // Leg 2 Rebooked Arc: ORD -> LHR (UA938 / Emerald)
        const ptsRebook = this.createGreatCircleArc(this.airportPositions.ORD, this.airportPositions.LHR, 12.5);
        this.curveRebook = new THREE.CatmullRomCurve3(ptsRebook);
        const geoRebook = new THREE.BufferGeometry().setFromPoints(ptsRebook);
        this.matRebook = new THREE.LineBasicMaterial({
            color: 0x1E6B4B,
            linewidth: 3,
            transparent: true,
            opacity: 0.0
        });
        this.lineRebook = new THREE.Line(geoRebook, this.matRebook);
        this.globeGroup.add(this.lineRebook);

        // Nonstop Alternative Arc: SFO -> LHR (UA901)
        const ptsNonstop = this.createGreatCircleArc(this.airportPositions.SFO, this.airportPositions.LHR, 15);
        this.curveNonstop = new THREE.CatmullRomCurve3(ptsNonstop);
        const geoNonstop = new THREE.BufferGeometry().setFromPoints(ptsNonstop);
        this.matNonstop = new THREE.LineBasicMaterial({
            color: 0x14213D,
            linewidth: 2.5,
            transparent: true,
            opacity: 0.0
        });
        this.lineNonstop = new THREE.Line(geoNonstop, this.matNonstop);
        this.globeGroup.add(this.lineNonstop);
    }

    createAircrafts() {
        // Glowing Airplane Mesh / Marker
        const planeGeo = new THREE.ConeGeometry(0.9, 2.2, 8);
        planeGeo.rotateX(Math.PI / 2);

        const planeMat1 = new THREE.MeshStandardMaterial({
            color: 0xB88A3B,
            emissive: 0x75531B,
            roughness: 0.3
        });
        this.aircraft1 = new THREE.Mesh(planeGeo, planeMat1);
        this.globeGroup.add(this.aircraft1);

        this.planeProgress = 0.15;
    }

    setState(newState, optionId = 'UA938') {
        this.state = newState;
        this.selectedOption = optionId;

        if (newState === 'ontime') {
            this.matSfoOrd.color.setHex(0xB88A3B);
            this.matOrdLhr.color.setHex(0xB88A3B);
            this.matRebook.opacity = 0.0;
            this.matNonstop.opacity = 0.0;
            this.ordPulseMat.opacity = 0.0;
            this.ordRingMat.color.setHex(0xB88A3B);
            this.ordDiscMat.color.setHex(0xB88A3B);
        } else if (newState === 'delayed') {
            // Disruption on UA1542
            this.matSfoOrd.color.setHex(0xB6382E);
            this.matOrdLhr.color.setHex(0xB6382E);
            this.ordRingMat.color.setHex(0xB6382E);
            this.ordDiscMat.color.setHex(0xB6382E);
            this.ordPulseMat.opacity = 0.8;
            this.matRebook.opacity = 0.0;
        } else if (newState === 'resolving' || newState === 'proposed') {
            this.matSfoOrd.color.setHex(0xB6382E);
            this.updateOptionHighlight(this.selectedOption);
        } else if (newState === 'approved') {
            this.matSfoOrd.color.setHex(0x1E6B4B);
            this.matOrdLhr.opacity = 0.2;
            this.matRebook.opacity = 1.0;
            this.matRebook.color.setHex(0x1E6B4B);
            this.ordPulseMat.opacity = 0.0;
            this.ordRingMat.color.setHex(0x1E6B4B);
            this.ordDiscMat.color.setHex(0x1E6B4B);
        } else if (newState === 'declined') {
            this.matSfoOrd.color.setHex(0xB6382E);
            this.matOrdLhr.color.setHex(0xB6382E);
            this.matRebook.opacity = 0.0;
        }
    }

    updateOptionHighlight(optId) {
        if (optId === 'UA901') {
            // Nonstop option
            this.matNonstop.opacity = 0.9;
            this.matRebook.opacity = 0.0;
            this.matOrdLhr.opacity = 0.2;
        } else {
            this.matNonstop.opacity = 0.0;
            this.matOrdLhr.opacity = 0.2;
            this.matRebook.opacity = 0.95;
            if (optId === 'UA938') {
                this.matRebook.color.setHex(0x1E6B4B); // Green
            } else if (optId === 'BA296') {
                this.matRebook.color.setHex(0xD97706); // Amber
            } else {
                this.matRebook.color.setHex(0xB6382E); // Red
            }
        }
    }

    setupEventListeners() {
        const dom = this.renderer.domElement;

        dom.addEventListener('mousedown', (e) => {
            this.isDragging = true;
            this.previousMousePosition = { x: e.clientX, y: e.clientY };
        });

        window.addEventListener('mouseup', () => {
            this.isDragging = false;
        });

        window.addEventListener('mousemove', (e) => {
            if (this.isDragging) {
                const deltaX = e.clientX - this.previousMousePosition.x;
                const deltaY = e.clientY - this.previousMousePosition.y;

                this.globeGroup.rotation.y += deltaX * 0.005;
                this.globeGroup.rotation.x += deltaY * 0.003;

                // Clamp vertical tilt
                this.globeGroup.rotation.x = Math.max(-0.4, Math.min(0.4, this.globeGroup.rotation.x));

                this.previousMousePosition = { x: e.clientX, y: e.clientY };
            } else {
                // Subtle Parallax
                const rect = dom.getBoundingClientRect();
                const x = (e.clientX - rect.left) / rect.width - 0.5;
                const y = (e.clientY - rect.top) / rect.height - 0.5;
                this.mouse.targetX = x * 0.15;
                this.mouse.targetY = y * 0.1;
            }
        });

        window.addEventListener('resize', () => {
            this.width = this.container.clientWidth;
            this.height = this.container.clientHeight || 240;
            this.camera.aspect = this.width / this.height;
            this.camera.updateProjectionMatrix();
            this.renderer.setSize(this.width, this.height);
        });
    }

    animate() {
        requestAnimationFrame(this.animate);

        const delta = this.clock.getDelta();
        const time = this.clock.getElapsedTime();

        // Smooth Parallax Damping
        if (!this.isDragging) {
            this.mouse.x += (this.mouse.targetX - this.mouse.x) * 0.05;
            this.mouse.y += (this.mouse.targetY - this.mouse.y) * 0.05;
            this.camera.position.x = this.mouse.x * 20;
            this.camera.position.y = 35 - this.mouse.y * 15;
            this.camera.lookAt(0, 5, 0);
        }

        // Pulse Animation for ORD disruption
        if (this.state === 'delayed' || this.state === 'resolving' || this.state === 'proposed') {
            const scale = 1.0 + (time * 1.8 % 1.5);
            const op = Math.max(0, 0.9 - (scale - 1.0) / 1.5);
            this.ordPulseMesh.scale.set(scale, scale, 1);
            this.ordPulseMat.opacity = op;
        }

        // Aircraft motion along curve
        this.planeProgress = (this.planeProgress + delta * 0.08) % 1.0;

        let activeCurve = this.curveSfoOrd;
        if (this.state === 'approved') {
            activeCurve = this.curveRebook;
        } else if (this.state === 'delayed') {
            activeCurve = this.curveSfoOrd;
        }

        const point = activeCurve.getPointAt(this.planeProgress);
        const nextPoint = activeCurve.getPointAt(Math.min(1.0, this.planeProgress + 0.02));

        this.aircraft1.position.copy(point);
        this.aircraft1.lookAt(nextPoint);

        this.renderer.render(this.scene, this.camera);
    }
}

window.FlightVisualizer3D = FlightVisualizer3D;
