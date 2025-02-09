#give dgl graphs, reaction features, meta. write them into lmdb file.
#1. check expend lmdb reasonably

#give dgl graphs, reaction features, meta. write them into lmdb file.
#1. check expend lmdb reasonably

from torch.utils.data import Dataset
from pathlib import Path
import numpy as np
import pickle
import lmdb
from torch.utils.data import random_split
import multiprocessing as mp
import os
import pickle
from tqdm import tqdm
import glob 


class LmdbDataset(Dataset):
    """
    Dataset class to 
    1. write Reaction networks objecs to lmdb
    2. load lmdb files
    """
    def __init__(self, config, transform=None):
        super(LmdbDataset, self).__init__()

        self.config = config
        self.path = Path(self.config["src"])

        #Get metadata in case
        #self.metadata_path = self.path.parent / "metadata.npz"
        self.env = self.connect_db(self.path)
    
        # If "length" encoded as ascii is present, use that
        # If there are additional properties, there must be length.
        length_entry = self.env.begin().get("length".encode("ascii"))
        if length_entry is not None:
            num_entries = pickle.loads(length_entry)
        else:
            # Get the number of stores data from the number of entries
            # in the LMDB
            num_entries = self.env.stat()["entries"]

        self._keys = list(range(num_entries))
        self.num_samples = num_entries
        
        #Get portion of total dataset
        self.sharded = False
        if "shard" in self.config and "total_shards" in self.config:
            self.sharded = True
            self.indices = range(self.num_samples)
            # split all available indices into 'total_shards' bins
            self.shards = np.array_split(
                self.indices, self.config.get("total_shards", 1)
            )
            # limit each process to see a subset of data based off defined shard
            self.available_indices = self.shards[self.config.get("shard", 0)]
            self.num_samples = len(self.available_indices)
            
        #TODO
        self.transform = transform

    def __len__(self):
        return self.num_samples

    def __getitem__(self, idx):
        # if sharding, remap idx to appropriate idx of the sharded set
        if self.sharded:
            idx = self.available_indices[idx]

        #!CHECK, _keys should be less then total numbers of keys as there are more properties.
        datapoint_pickled = self.env.begin().get(
                f"{self._keys[idx]}".encode("ascii")
            )
        
        data_object = pickle.loads(datapoint_pickled)

        #TODO
        if self.transform is not None:
            data_object = self.transform(data_object)

        return data_object

    def connect_db(self, lmdb_path=None):
        env = lmdb.open(
            str(lmdb_path),
            subdir=False,
            readonly=True,
            lock=False,
            readahead=True,
            meminit=False,
            max_readers=1,
        )
        return env

    def close_db(self):
        if not self.path.is_file():
            for env in self.envs:
                env.close()
        else:
            self.env.close()

    def get_metadata(self, num_samples=100):
        pass

    @property
    def dtype(self):
        dtype = self.env.begin().get("dtype".encode("ascii"))
        return  pickle.loads(dtype)
            
    @property
    def feature_size(self):
        feature_size = self.env.begin().get("feature_size".encode("ascii"))
        return pickle.loads(feature_size)

    @property
    def feature_name(self):
        feature_name = self.env.begin().get("feature_name".encode("ascii"))
        return pickle.loads(feature_name)



def divide_to_list(a, b):
    quotient = a // b
    remainder = a % b

    result = []
    for i in range(b):
        increment = 1 if i < remainder else 0
        result.append(quotient + increment)

    return result

def cleanup_lmdb_files(directory, pattern):
    """
    Cleans up files matching the given pattern in the specified directory.
    """
    file_list = glob.glob(os.path.join(directory, pattern))

    for file_path in file_list:
        try:
            os.remove(file_path)
            print(f"Deleted file: {file_path}")
        except OSError as e:
            print(f"Error deleting file: {file_path}. {str(e)}")

def CRNs2lmdb(CRNsDb, config):
    
    os.makedirs(os.path.join(config["out_path"]), exist_ok=True)

    db_paths = [
        os.path.join(config["out_path"], "_tmp_data.%04d.lmdb" % i)
        for i in range(config["num_workers"])
    ]

    pool = mp.Pool(config["num_workers"])

    dataset_chunked = random_split(CRNsDb, divide_to_list(len(CRNsDb), config["num_workers"]))
    
    
    #total numbers of properties equal to 3+1 (length)
    meta_keys = {
                "dtype" : CRNsDb.dtype,
                "feature_size":CRNsDb.feature_size,
                "feature_name":CRNsDb.feature_name
                }

    mp_args = [
        (
            db_paths[i],
            dataset_chunked[i],
            i,
            meta_keys
        )
        for i in range(config["num_workers"])
    ]

    # for key, value in meta_keys.items():
    #     print(key,value)

    #Property should write to subset as well, as one may train them separately
    #TODO imap faster?
    pool.map(write_crns_to_lmdb, mp_args)
    pool.close()

    # Merge LMDB files
    merge_lmdbs(db_paths, config["out_path"], config["output_file"])
    cleanup_lmdb_files(config["out_path"], "_tmp_data*")

    
def write_crns_to_lmdb(mp_args):
    #pid is idx of workers.
    db_path, samples, pid, meta_keys = mp_args

    db = lmdb.open(
        db_path,
        map_size=1099511627776 * 2,
        subdir=False,
        meminit=False,
        map_async=True,
    )

    pbar = tqdm(
        total=len(samples),
        position=pid,
        desc=f"Worker {pid}: Writing CRNs Objects into LMDBs",
    )
    
    #write indexed samples
    idx = 0
    for sample in samples:
        txn=db.begin(write=True)
        txn.put(
            f"{idx}".encode("ascii"),
            pickle.dumps(sample, protocol=-1),
        )
        idx += 1
        pbar.update(1)
        txn.commit()
        
    #write properties
    txn=db.begin(write=True)
    txn.put("length".encode("ascii"), pickle.dumps(len(samples), protocol=-1))
    txn.commit()
    
    for key, value in meta_keys.items():
        txn=db.begin(write=True)
        txn.put(key.encode("ascii"), pickle.dumps(value, protocol=-1))
        txn.commit()
    
    db.sync()
    db.close()


def merge_lmdbs(db_paths, out_path, output_file):
    """
    merge lmdb files and reordering indexes.
    """
    env_out = lmdb.open(
        os.path.join(out_path, output_file),
        map_size=1099511627776 * 2,
        subdir=False,
        meminit=False,
        map_async=True,
    )
    
    
    idx = 0
    for db_path in db_paths:
        env_in = lmdb.open(
            str(db_path),
            subdir=False,
            readonly=True,
            lock=False,
            readahead=True,
            meminit=False,
        )
        
        #should set indexes so that properties do not writtent down as well.
        with env_out.begin(write=True) as txn_out, env_in.begin(write=False) as txn_in:
            cursor = txn_in.cursor()
            for key, value in cursor:
                #write indexed samples
                try:
                    int(key.decode("ascii"))
                    txn_out.put(
                    f"{idx}".encode("ascii"),
                    value,
                    )
                    idx+=1
                    #print(idx)
                #write properties
                except ValueError:
                    txn_out.put(
                        key,
                        value
                    )
        env_in.close()
    
    #update length
    txn_out=env_out.begin(write=True)
    txn_out.put("length".encode("ascii"), pickle.dumps(idx, protocol=-1))
    txn_out.commit()
        
    env_out.sync()
    env_out.close()
