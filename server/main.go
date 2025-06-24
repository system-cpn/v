package main

import (
	"fmt"
	"log"
	"net"
	"sync"

	mqtt "github.com/eclipse/paho.mqtt.golang"
)

var (
	clients    = make(map[net.Conn]bool)
	clientsMux = sync.RWMutex{}
)

func main() {
	// Start TCP server
	go startTCPServer()

	// Start MQTT client
	startMQTTClient()

	select {} // Keep running
}

func startTCPServer() {
	listener, err := net.Listen("tcp", "127.0.0.1:6000")
	if err != nil {
		log.Fatal("TCP server failed:", err)
	}
	defer listener.Close()

	log.Println("TCP server listening on 127.0.0.1:6000")

	for {
		conn, err := listener.Accept()
		if err != nil {
			log.Println("Accept failed:", err)
			continue
		}

		clientsMux.Lock()
		clients[conn] = true
		clientsMux.Unlock()

		log.Println("Client connected:", conn.RemoteAddr())

		go handleClient(conn)
	}
}

func handleClient(conn net.Conn) {
	defer func() {
		clientsMux.Lock()
		delete(clients, conn)
		clientsMux.Unlock()
		conn.Close()
		log.Println("Client disconnected:", conn.RemoteAddr())
	}()

	// Keep connection alive
	buffer := make([]byte, 1024)
	for {
		_, err := conn.Read(buffer)
		if err != nil {
			break
		}
	}
}

func broadcastToClients(message string) {
	clientsMux.RLock()
	defer clientsMux.RUnlock()

	for conn := range clients {
		_, err := conn.Write([]byte(message + "\n"))
		if err != nil {
			log.Println("Write to client failed:", err)
		}
	}
}

func startMQTTClient() {
	opts := mqtt.NewClientOptions()
	opts.AddBroker("tcp://mqtt.cpnz.net:1883")
	opts.SetClientID("tcp-service")
	opts.SetUsername("loki")
	opts.SetPassword("nyamukDemamBerdarah224411")

	client := mqtt.NewClient(opts)
	if token := client.Connect(); token.Wait() && token.Error() != nil {
		log.Fatal("MQTT connection failed:", token.Error())
	}

	log.Println("Connected to MQTT broker")

	// Subscribe to topic
	if token := client.Subscribe("ais/raw", 0, messageHandler); token.Wait() && token.Error() != nil {
		log.Fatal("MQTT subscribe failed:", token.Error())
	}

	log.Println("Subscribed to MQTT topic: ais/raw")
}

func messageHandler(client mqtt.Client, msg mqtt.Message) {
	message := fmt.Sprintf("MQTT: %s", string(msg.Payload()))
	log.Println("Received:", message)
	broadcastToClients(message)
}
