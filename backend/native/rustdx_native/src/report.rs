use rustdx_complete::tcp::{Tcp, send_recv_decompress};

pub const CHUNK_SIZE: u32 = 30_000;
pub const MAX_FILE_SIZE: usize = 128_000_000;

fn request(filename: &str, offset: u32) -> Result<Vec<u8>, String> {
    if filename != "tdxfin/gpcw.txt"
        && !(filename.len() == 23
            && filename.starts_with("tdxfin/gpcw")
            && filename.ends_with(".zip")
            && filename.as_bytes()[11..19].iter().all(u8::is_ascii_digit))
    {
        return Err("invalid rustdx financial filename".to_string());
    }
    let mut packet = vec![0x0c, 0x12, 0x34, 0, 0, 0];
    packet.extend_from_slice(&110_u16.to_le_bytes());
    packet.extend_from_slice(&110_u16.to_le_bytes());
    packet.extend_from_slice(&0x06b9_u16.to_le_bytes());
    packet.extend_from_slice(&offset.to_le_bytes());
    packet.extend_from_slice(&CHUNK_SIZE.to_le_bytes());
    packet.extend_from_slice(filename.as_bytes());
    packet.resize(120, 0);
    Ok(packet)
}

fn chunk(response: &[u8]) -> Result<&[u8], String> {
    let length = response
        .get(..4)
        .ok_or_else(|| "truncated rustdx financial chunk header".to_string())?;
    let length = u32::from_le_bytes(length.try_into().unwrap()) as usize;
    if length > CHUNK_SIZE as usize || response.len() != length + 4 {
        return Err("invalid rustdx financial chunk length".to_string());
    }
    Ok(&response[4..])
}

pub fn download(tcp: &mut Tcp, filename: &str, max_bytes: usize) -> Result<Vec<u8>, String> {
    if max_bytes == 0 || max_bytes > MAX_FILE_SIZE {
        return Err("invalid rustdx financial file size limit".to_string());
    }
    let mut output = Vec::new();
    loop {
        let packet = request(filename, output.len() as u32)?;
        let response = send_recv_decompress(tcp, &packet, "financial report")
            .map_err(|error| error.to_string())?;
        let data = chunk(&response)?;
        if data.is_empty() {
            return Ok(output);
        }
        if output.len() + data.len() > max_bytes {
            return Err("rustdx financial file exceeds size limit".to_string());
        }
        output.extend_from_slice(data);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn packet_matches_report_protocol() {
        let packet = request("tdxfin/gpcw20260630.zip", 60_000).unwrap();
        assert_eq!(packet.len(), 120);
        assert_eq!(&packet[6..12], &[110, 0, 110, 0, 0xb9, 6]);
        assert_eq!(&packet[12..16], &60_000_u32.to_le_bytes());
        assert_eq!(&packet[16..20], &30_000_u32.to_le_bytes());
        assert_eq!(&packet[20..43], b"tdxfin/gpcw20260630.zip");
    }

    #[test]
    fn rejects_paths_and_invalid_chunks() {
        assert!(request("../secrets", 0).is_err());
        assert!(chunk(&[1, 0, 0]).is_err());
        assert!(chunk(&[2, 0, 0, 0, 1]).is_err());
        assert!(chunk(&[0, 0, 0, 0]).unwrap().is_empty());
        assert_eq!(chunk(&[1, 0, 0, 0, 42]).unwrap(), &[42]);
    }
}
