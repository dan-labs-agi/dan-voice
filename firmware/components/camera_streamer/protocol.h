/**
 * protocol.h
 *
 * Defines:
 *   1. The wire format used to send frames ([4-byte length][JPEG bytes]).
 *   2. An abstract ITransport interface so the streaming logic in
 *      streamer.cpp never talks to a raw socket directly. Today only
 *      TcpTransport exists; adding UdpTransport or a UsbTransport later
 *      means implementing this interface — streamer.cpp does not change.
 */

#ifndef PROTOCOL_H
#define PROTOCOL_H

#include <cstddef>
#include <cstdint>

// ----------------------------------------------------------------------------
// Transport abstraction
// ----------------------------------------------------------------------------
// Any concrete transport (TCP today, UDP or USB CDC tomorrow) implements
// this interface. The streaming task only ever depends on ITransport*.
class ITransport {
public:
    virtual ~ITransport() {}

    // Blocking-with-timeout initialization / listen setup. Returns true on
    // success. Safe to call again after end() to restart the transport.
    virtual bool begin() = 0;

    // Tears down any open sockets/handles. Safe to call multiple times.
    virtual void end() = 0;

    // Returns true if a client is currently connected and ready for writes.
    virtual bool isClientConnected() = 0;

    // Blocks (up to an internal accept timeout) waiting for a client to
    // connect. Returns true once one is connected. Should be polled from a
    // loop so Wi-Fi state and shutdown requests can be checked between
    // attempts.
    virtual bool waitForClient(uint32_t timeoutMs) = 0;

    // Writes exactly `len` bytes from `data`. Returns true only if the full
    // payload was written successfully. On any failure the caller should
    // treat the client as disconnected and call end()/waitForClient() again.
    virtual bool writeExact(const uint8_t* data, size_t len) = 0;

    // Human-readable transport name, for logging ("TCP", "UDP", "USB").
    virtual const char* name() const = 0;
};

// ----------------------------------------------------------------------------
// Wire format helpers
// ----------------------------------------------------------------------------
namespace Protocol {

// Serializes a frame length as a 4-byte little-endian prefix into `out`
// (caller-provided 4-byte buffer).
void encodeLength(uint32_t length, uint8_t out[4]);

// Parses a 4-byte little-endian length prefix.
uint32_t decodeLength(const uint8_t in[4]);

// Sends one full frame (length prefix + JPEG payload) over the given
// transport. Returns true only if both the header and the payload were
// written successfully.
bool sendFrame(ITransport& transport, const uint8_t* jpegData, size_t jpegLen);

} // namespace Protocol

#endif // PROTOCOL_H
