/**
 * protocol.cpp
 *
 * Implements the wire-format helpers declared in protocol.h, plus the
 * concrete TcpTransport (declared here since only streamer.cpp needs it,
 * via the factory function createTcpTransport()).
 */

#include "protocol.h"
#include "config.h"
#include <WiFi.h>
#include <lwip/sockets.h>
#include <lwip/netdb.h>
#include <errno.h>

// ----------------------------------------------------------------------------
// TcpTransport: concrete ITransport implementation using a raw lwIP socket.
// A raw socket (rather than WiFiClient/WiFiServer) is used so we have direct
// control over SO_SNDBUF, TCP_NODELAY, and send timeouts — all of which
// matter for sustained high-frame-rate throughput.
// ----------------------------------------------------------------------------
class TcpTransport : public ITransport {
public:
    TcpTransport() : m_listenFd(-1), m_clientFd(-1) {}

    bool begin() override {
        m_listenFd = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        if (m_listenFd < 0) {
            Serial.printf("[TCP] socket() failed, errno=%d\n", errno);
            return false;
        }

        int reuse = 1;
        setsockopt(m_listenFd, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));

        struct sockaddr_in addr;
        memset(&addr, 0, sizeof(addr));
        addr.sin_family = AF_INET;
        addr.sin_addr.s_addr = htonl(INADDR_ANY);
        addr.sin_port = htons(TCP_SERVER_PORT);

        if (bind(m_listenFd, (struct sockaddr*)&addr, sizeof(addr)) != 0) {
            Serial.printf("[TCP] bind() failed, errno=%d\n", errno);
            close(m_listenFd);
            m_listenFd = -1;
            return false;
        }

        if (listen(m_listenFd, TCP_LISTEN_BACKLOG) != 0) {
            Serial.printf("[TCP] listen() failed, errno=%d\n", errno);
            close(m_listenFd);
            m_listenFd = -1;
            return false;
        }

        // Non-blocking accept loop is implemented via select() with a
        // timeout inside waitForClient(), so mark the listen socket
        // non-blocking.
        int flags = fcntl(m_listenFd, F_GETFL, 0);
        fcntl(m_listenFd, F_SETFL, flags | O_NONBLOCK);

        Serial.printf("[TCP] Listening on port %d\n", TCP_SERVER_PORT);
        return true;
    }

    void end() override {
        if (m_clientFd >= 0) {
            close(m_clientFd);
            m_clientFd = -1;
        }
        if (m_listenFd >= 0) {
            close(m_listenFd);
            m_listenFd = -1;
        }
    }

    bool isClientConnected() override {
        return m_clientFd >= 0;
    }

    bool waitForClient(uint32_t timeoutMs) override {
        if (m_listenFd < 0) {
            return false;
        }
        if (m_clientFd >= 0) {
            return true; // already have a client
        }

        fd_set readSet;
        FD_ZERO(&readSet);
        FD_SET(m_listenFd, &readSet);

        struct timeval tv;
        tv.tv_sec = timeoutMs / 1000;
        tv.tv_usec = (timeoutMs % 1000) * 1000;

        int ready = select(m_listenFd + 1, &readSet, nullptr, nullptr, &tv);
        if (ready <= 0) {
            return false; // timeout or error, no client yet
        }

        struct sockaddr_in clientAddr;
        socklen_t clientLen = sizeof(clientAddr);
        int fd = accept(m_listenFd, (struct sockaddr*)&clientAddr, &clientLen);
        if (fd < 0) {
            return false;
        }

        // Configure the client socket for low-latency streaming.
        int nodelay = TCP_NODELAY_ENABLED;
        setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &nodelay, sizeof(nodelay));

        int sndbuf = TCP_SOCKET_SNDBUF_BYTES;
        setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof(sndbuf));

        struct timeval sendTimeout;
        sendTimeout.tv_sec = TCP_SOCKET_SEND_TIMEOUT_S;
        sendTimeout.tv_usec = 0;
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &sendTimeout, sizeof(sendTimeout));

        m_clientFd = fd;

        char ipStr[INET_ADDRSTRLEN];
        inet_ntop(AF_INET, &clientAddr.sin_addr, ipStr, sizeof(ipStr));
        Serial.printf("[TCP] Client connected: %s:%d\n", ipStr, ntohs(clientAddr.sin_port));

        return true;
    }

    bool writeExact(const uint8_t* data, size_t len) override {
        if (m_clientFd < 0) {
            return false;
        }
        size_t sent = 0;
        while (sent < len) {
            ssize_t n = send(m_clientFd, data + sent, len - sent, 0);
            if (n < 0) {
                if (errno == EAGAIN || errno == EWOULDBLOCK) {
                    // Timed out per SO_SNDTIMEO; treat as a dead client.
                    Serial.println("[TCP] send() timed out, dropping client");
                } else {
                    Serial.printf("[TCP] send() failed, errno=%d\n", errno);
                }
                disconnectClient();
                return false;
            }
            if (n == 0) {
                disconnectClient();
                return false;
            }
            sent += (size_t)n;
        }
        return true;
    }

    const char* name() const override {
        return "TCP";
    }

private:
    void disconnectClient() {
        if (m_clientFd >= 0) {
            close(m_clientFd);
            m_clientFd = -1;
            Serial.println("[TCP] Client disconnected");
        }
    }

    int m_listenFd;
    int m_clientFd;
};

// Factory function exposed to streamer.cpp so it can own an ITransport*
// without needing to know the concrete class. This is the seam where a
// future createUdpTransport() or createUsbTransport() would be added.
ITransport* createTcpTransport() {
    static TcpTransport instance;
    return &instance;
}

// ----------------------------------------------------------------------------
// Wire-format helpers
// ----------------------------------------------------------------------------
namespace Protocol {

void encodeLength(uint32_t length, uint8_t out[4]) {
    out[0] = (uint8_t)(length & 0xFF);
    out[1] = (uint8_t)((length >> 8) & 0xFF);
    out[2] = (uint8_t)((length >> 16) & 0xFF);
    out[3] = (uint8_t)((length >> 24) & 0xFF);
}

uint32_t decodeLength(const uint8_t in[4]) {
    return (uint32_t)in[0] |
           ((uint32_t)in[1] << 8) |
           ((uint32_t)in[2] << 16) |
           ((uint32_t)in[3] << 24);
}

bool sendFrame(ITransport& transport, const uint8_t* jpegData, size_t jpegLen) {
    if (jpegLen == 0 || jpegLen > PROTOCOL_MAX_FRAME_BYTES) {
        Serial.printf("[Protocol] Refusing to send invalid frame size=%u\n", (unsigned)jpegLen);
        return false;
    }

    uint8_t header[PROTOCOL_LENGTH_PREFIX_BYTES];
    encodeLength((uint32_t)jpegLen, header);

    if (!transport.writeExact(header, sizeof(header))) {
        return false;
    }
    if (!transport.writeExact(jpegData, jpegLen)) {
        return false;
    }
    return true;
}

} // namespace Protocol
