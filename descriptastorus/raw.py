"""Raw storage of integer and floating point data.

This provides fast random access to indexed data.

r = RawStore(directory)
row = r.get(0)
row = r.get(100000)

Data can be updated using putRow when opened in append mode.
"""
from __future__ import print_function
import pickle, numpy, os, mmap, struct, sys
import logging

# raw stores are little endian!

class Mode:
    READONLY = 0
    WRITE = 1
    APPEND = 2
    READONCE = 3 # read through the file once...
    
class RawStoreIter:
    def __init__(self, raw):
        self.raw = raw
        self.i = -1
    def next(self):
        self.i += 1
        if self.i >= self.raw.N:
            raise StopIteration()
        return self.raw.get(self.i)

def convert_string( v ):
    if type(v) == str:
        return str.encode(v)
    return v
                
    
class RawStore:
    def __init__(self, directory, mode=Mode.READONLY):
        """Raw storage engine
        directory = existing directory to read or prepare the raw storage
        if N is None, an existing directory is created with N entries"""
        if not os.path.exists(directory):
            raise IOError("Directory %s for raw store does not exist"%
                          directory)
        self.directory = directory
        with open(os.path.join(directory, "__rawformat__"), 'rb') as rawformat:
            self.__dict__.update(pickle.load(rawformat))
            
        fname = self.fname = os.path.join(directory, "__store___")

        self._f = None
        self.mode = mode
        self._openfile()

    def _openfile(self):
        fname = self.fname
        mode = self.mode
        if mode == Mode.APPEND:
            self._f = open(fname, 'rb+')
            access = None
        if mode == Mode.READONLY:
            self._f = open(fname, 'rb')
            access = mmap.ACCESS_READ # shared by default
        elif mode == Mode.READONCE:
            self.f = open(fname, 'rb')
        else:
            self._f = open(fname, 'r+b')
            access = mmap.ACCESS_WRITE

        if self._f is not None:
            self.f = mmap.mmap(self._f.fileno(), 0, access=access)
        else:
            self.f = self._f
        
    def close(self):
        self.f.close()
        self._f.close()

    def __len__(self):
        return self.N
    
    def __iter__(self):
        return RawStoreIter(self)

    def appendBlankRows(self, M):
        """Adds M blank rows to the store (must be opened in append mode)"""
        if self.mode != Mode.APPEND:
            raise IOError("Storage must be opened in append mode to add blank rows")
        self.close()
        _f = open(self.fname, 'rb+')        
        if M < 1:
            raise ValueError("The value of M must be positive, not %r"%M)
        self.close()
        
        logging.info("Seeking to %s",  self.rowbytes * (self.N + M))
        _f.seek(self.rowbytes * (self.N + M))
        _f.write(b'\0')
        self.N += M
        _f.close()
        logging.info("Filesize is %s", os.path.getsize(self.fname))
        
        opts = None
        with open(os.path.join(self.directory, "__rawformat__"), 'rb') as rawformat:
            opts = pickle.load(rawformat)
        opts['N'] = self.N
        with open(os.path.join(self.directory, "__rawformat__"), 'wb') as rawformat:
            pickle.dump(opts, rawformat)
        self._openfile()
        
    def getDict(self, idx):
        """{colname:value, ...} Return the row at idx as a dictionary"""
        return {name:v for name, v in zip(self.colnames, self.get(idx))}
        
    def get(self, idx):
        """Return the row at idx"""
        if idx >= self.N or idx < 0:
            raise IndexError("Index out of range %s (0 < %s)",
                             idx, self.N)
        offset = idx * self.rowbytes
        try:
            self.f.seek(offset,0)
        except ValueError:
            print("Could not seek to index %d at offset %f offset"%(
                idx, offset), file=sys.stderr)
            raise IndexError("out or range %d"%idx)
        
        _bytes = self.f.read(self.rowbytes)
        res = struct.unpack(self.pack_format, _bytes)
        if "s" not in self.pack_format:
            return res
        else:
            return tuple([ str(x).replace("\x00","")
                           if isinstance(x, (str, bytes)) else x for x in res ])

    def getColByIdx(self, column):
        """Return the data in the entire column (lazy generator)"""
        # figure out the column
        
        # figure out how many bytes to skip per row
        skip_format = self.pack_format[:column]
        offset = len(struct.pack(skip_format,
                                 *[dtype(0)
                                   for dtype in self.dtypes[:column]]))


        # figure out the bytes to read via the format for the row
        pack_format = self.pack_format[column]
        nbytes = len(struct.pack(pack_format, self.dtypes[column](0)))

        skip = offset - nbytes

        # seek to the first column
        self.f.seek(offset,0)
        # compute the next entry
        offset += self.rowbytes
        while 1:
            bytes = self.f.read(nbytes)
            if bytes == '':
                raise StopIteration
            try:
                yield struct.unpack(pack_format, bytes)
            except struct.error:
                if len(bytes) != nbytes:
                    raise StopIteration
                raise

            try:
                self.f.seek(offset, 0)
            except ValueError:
                raise StopIteration

            
            offset += self.rowbytes
            
    def getCol(self, column_name):
        """Return the column addressed by column_name
        throws IndexError if the column doesn't exist."""
        idx = self.colnames.index(column_name)
        return self.getColByIdx(idx)

    def putRow(self, idx, row):
        """Put data row in to row idx.
        Checks to see if the data in v is compatible with the column
        formats.  Throws ValueError on failure."""
        if idx >= self.N:
            raise IndexError("Attempting to write index %s, raw store only has %d rows"%(
                idx, self.N))
        
        if len(row) != len(self.dtypes):
            raise ValueError(
                "data value only has %s entries, "
                "raw store has %s columns"%(
                    len(row), len(self.dtypes)))

        # checks row datatypes
        try:
            v = [dtype(v) for dtype,v in zip(self.dtypes, row)]
        except TypeError as e:
            # we might have None's here
            message = [str(e)]
            for i,(dtype,v) in enumerate(zip(self.dtypes, row)):
                try: dtype(v)
                except:
                    message.append("\tFor column %r can't convert %r to %s"%(
                        self.colnames[i],
                        v,
                        dtype))
            raise TypeError("\n".join(message))
        
        offset = idx * self.rowbytes
        self.f.seek(offset,0)
        try:
            bytes = struct.pack(self.pack_format, *[convert_string(x) for x  in row])
        except struct.error:
            logging.exception("Can't write row %r\ntypes: %r\nformat: %r",
                              row,
                              self.dtypes,
                              self.pack_format)
            raise
        try:
            self.f.write(bytes)
        except Exception as e:
            logging.error("Attempting to write to offset: %s", offset)
            logging.error("Rowsize: %s", self.rowbytes)
            logging.error("Row: %s", idx)
            logging.error("Max Row: %s", self.N)
            logging.error("Filesize is %s",
                           os.path.getsize(self.fname))
            raise

    def write(self, row):
        """Writes row with no datatype checking to the end of the file.
        Do not use with putRow/getRow
        Rows must be written in the correct order
        (strings datatypes are currently an issue)
        """
        bytes = struct.pack(self.pack_format, *row)
        self.f.write(bytes)

def str_store(s):
    return str.encode(str(s))
        
def MakeStore(cols, N, directory, checkDirectoryExists=True):
    if not os.path.exists(directory):
        os.mkdir(directory)
    else:
        if checkDirectoryExists:
            raise IOError("Directory %r for raw store already exists"%directory)

    if not N:
        raise ValueError(
            "When creating a RawStore the total number of elements "
            "must be known,\nplease specify N=?")


    types = []
    names = []
    dtypes = []
    for name, dtype in cols:
        names.append(name)
        if dtype == numpy.int32:
            type = "i"
            dtypes.append(int)
        elif dtype == numpy.int64:
            type = "q"
            dtypes.append(int)
        elif dtype == numpy.uint8:
            type = "B"
            dtypes.append(int)
        elif dtype == numpy.uint16:
            type = "H"
            dtypes.append(int)
        elif dtype == numpy.uint32:
            type = "I"
            dtypes.append(int)
        elif dtype == numpy.uint64:
            type = "Q"
            dtypes.append(int)
        elif dtype == numpy.float32:
            type = "f"
            dtypes.append(float)
        elif dtype == numpy.float64:
            type = "d"
            dtypes.append(float)
        elif dtype == numpy.bool:
            type = "?"
            dtypes.append(bool)
        elif hasattr(dtype, 'type'): # for strings
            if dtype.type == numpy.string_:
                size = dtype.itemsize
                type = "%ss"%size
                dtypes.append(str_store)
        else:
            raise ValueError("Unhandled numpy type %s"%dtype)

        types.append(type)
                     

    pack_format = "".join(types)
    rowbytes = len(struct.pack(pack_format,
                               *[d(0) for d in dtypes]))
    with open(os.path.join(directory, "__rawformat__"), 'wb') as rawformat:
        pickle.dump(
            {'rowbytes':rowbytes,
             'pack_format':pack_format,
             'colnames': names,
             'dtypes': dtypes,
             'N': N,
         }, rawformat)

    # Make the storage
    fname = os.path.join(directory, "__store___")
    with open(fname, 'wb') as f:
        f.seek(rowbytes*N)
        f.write(b'\0')

    # return the store
    return RawStore(directory, Mode.WRITE)
    
    
    
