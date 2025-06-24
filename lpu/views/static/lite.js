function cakrawalaApp() {
    return {
        cameras: window.cameras || [],
        recordings: window.recordings || {},
        selectedDate: window.selectedDate || '',
        currentMonth: new Date().getMonth(),
        currentYear: new Date().getFullYear(),
        playingVideos: {},
        showVideoModal: false,
        currentPlaylist: [],
        currentVideoIndex: 0,
        modalTitle: '',
        allRecordingDates: new Set(),

        monthNames: ["January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December"],
        daysOfWeek: ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'],

        init() {
            this.setupStreams();
            this.loadRecordingDates();
        },

        get formattedSelectedDate() {
            const date = new Date(this.selectedDate);
            return date.toLocaleDateString('en-US', { day: 'numeric', month: 'long', year: 'numeric' });
        },

        get hasRecordings() {
            return this.recordings && Object.keys(this.recordings).length > 0 && 
                   Object.values(this.recordings).some(camera => Object.keys(camera).length > 0);
        },

        get calendarDays() {
            const days = [];
            const firstDay = new Date(this.currentYear, this.currentMonth, 1).getDay();
            const daysInMonth = new Date(this.currentYear, this.currentMonth + 1, 0).getDate();
            const today = new Date();

            // Empty cells before month starts
            for (let i = 0; i < firstDay; i++) {
                days.push({ day: null, key: `empty-${i}` });
            }

            // Days of the month
            for (let day = 1; day <= daysInMonth; day++) {
                const dateStr = `${this.currentYear}-${String(this.currentMonth + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
                const classes = [];

                if (dateStr === this.selectedDate) classes.push('selected');
                if (day === today.getDate() && this.currentMonth === today.getMonth() && this.currentYear === today.getFullYear()) {
                    classes.push('today');
                }
                if (this.allRecordingDates.has(dateStr)) classes.push('has-recordings');

                days.push({
                    day,
                    dateStr,
                    classes: classes.join(' '),
                    key: dateStr
                });
            }

            return days;
        },

        getCameraColumnClass() {
            if (this.cameras.length === 1) return 'column is-12';
            if (this.cameras.length === 2) return 'column is-6';
            return 'column is-6-tablet is-4-desktop is-3-widescreen';
        },

        getPlayIcon(camera) {
            return this.playingVideos[camera] ? 'fas fa-play' : 'fas fa-pause';
        },

        setupStreams() {
            this.$nextTick(() => {
                this.cameras.forEach(camera => {
                    this.setupWebRTCStream(camera);
                });
            });
        },

        setupWebRTCStream(camera) {
            const container = document.getElementById(`video-${camera}`).parentElement;
            const videoElement = document.getElementById(`video-${camera}`);
            
            // Hide the original video element
            videoElement.style.display = 'none';
            
            // Create custom WebRTC video element
            const webrtcPlayer = document.createElement('video-rtc');
            webrtcPlayer.id = `webrtc-${camera}`;
            webrtcPlayer.style.position = 'absolute';
            webrtcPlayer.style.top = '0';
            webrtcPlayer.style.left = '0';
            webrtcPlayer.style.width = '100%';
            webrtcPlayer.style.height = '100%';
            
            // Set the WebSocket source (go2rtc WebSocket API)
            webrtcPlayer.src = `ws://localhost:1984/api/ws?src=${camera}`;
            
            // Insert before the video element
            container.insertBefore(webrtcPlayer, videoElement);
        },

        togglePlay(camera) {
            // For WebRTC streams, we can't easily control play/pause
            // The go2rtc player handles this internally
            this.playingVideos[camera] = !this.playingVideos[camera];
        },

        toggleFullscreen(camera) {
            const webrtcPlayer = document.getElementById(`webrtc-${camera}`);
            if (webrtcPlayer) {
                if (document.fullscreenElement) {
                    document.exitFullscreen();
                } else {
                    webrtcPlayer.requestFullscreen();
                }
            }
        },

        async loadRecordingDates() {
            try {
                const response = await fetch('/api/recordings/available');
                if (response.ok) {
                    const availableDates = await response.json();
                    this.allRecordingDates = new Set(availableDates);
                }
            } catch (error) {
                console.error('Error loading recording dates:', error);
                this.allRecordingDates = new Set();
            }
        },

        previousMonth() {
            if (this.currentMonth === 0) {
                this.currentMonth = 11;
                this.currentYear--;
            } else {
                this.currentMonth--;
            }
        },

        nextMonth() {
            if (this.currentMonth === 11) {
                this.currentMonth = 0;
                this.currentYear++;
            } else {
                this.currentMonth++;
            }
        },

        async selectDate(dateStr) {
            this.selectedDate = dateStr;
            
            try {
                const response = await fetch(`/api/recordings/${dateStr}`);
                this.recordings = await response.json();
            } catch (error) {
                console.error('Failed to load recordings:', error);
                this.recordings = {};
            }
        },

        playRecording(camera, hour, startIndex) {
            const hourRecordings = this.recordings[camera][hour];
            if (!hourRecordings || hourRecordings.length === 0) return;

            this.currentPlaylist = hourRecordings;
            this.currentVideoIndex = startIndex;
            this.modalTitle = `${camera.toUpperCase()} - ${hour}:00`;
            this.showVideoModal = true;
            
            this.$nextTick(() => {
                this.loadCurrentVideo();
            });
        },

        loadCurrentVideo() {
            const video = this.$refs.modalVideo;
            if (this.currentVideoIndex >= 0 && this.currentVideoIndex < this.currentPlaylist.length) {
                const recording = this.currentPlaylist[this.currentVideoIndex];
                video.src = `/api/video/${recording.filename}`;
                video.load();
            }
        },

        playPrevious() {
            if (this.currentVideoIndex > 0) {
                this.currentVideoIndex--;
                this.loadCurrentVideo();
            }
        },

        playNext() {
            if (this.currentVideoIndex < this.currentPlaylist.length - 1) {
                this.currentVideoIndex++;
                this.loadCurrentVideo();
            }
        },

        downloadCurrentVideo() {
            if (this.currentVideoIndex >= 0 && this.currentVideoIndex < this.currentPlaylist.length) {
                const recording = this.currentPlaylist[this.currentVideoIndex];
                const link = document.createElement('a');
                link.href = `/api/video/${recording.filename}`;
                link.download = recording.filename;
                document.body.appendChild(link);
                link.click();
                document.body.removeChild(link);
            }
        },

        closeVideoModal() {
            this.showVideoModal = false;
            const video = this.$refs.modalVideo;
            video.pause();
            video.src = '';
        }
    }
}